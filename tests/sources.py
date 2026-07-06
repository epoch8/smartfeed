"""Shared test harness for dedup / pagination correctness.

The existing test sources are mostly non-stateful (they ignore ``next_page`` and
re-emit the same page forever), which is exactly why the dedup/pagination bugs
were not caught. This module provides:

  * ``ScriptedSource`` -- a *stateful*, offset-paginated source over a FINITE
    pool. The pool may contain repeated ids (cross-page duplicates). It records
    every id it emits and how many times it was called, so tests can assert
    coverage ("no unique item was lost") and fetch counts ("cold build fetched
    once"). Finiteness is what lets us prove "no loss": drain to exhaustion and
    compare the union of pages against the known unique set.

  * pool factories (unique / duplicate-injected / overlapping).

  * a ``drain`` helper that pages to exhaustion with an infinite-loop guard, plus
    invariant assertions (coverage / no-duplicates / page-fullness).

Cursor convention for ScriptedSource: ``{"pos": <int offset into pool>}``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.wrapper import Wrapper, WrapperCache, WrapperDedup, WrapperRerank
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

class ScriptedSource:
    """Stateful, offset-paginated source over a finite pool of item dicts."""

    def __init__(self, pool: List[Dict], latency: float = 0.0, id_key: str = "id"):
        self.pool = [dict(it) for it in pool]
        self.latency = latency
        self.id_key = id_key
        self.calls = 0
        self.emitted: List[Any] = []

    @property
    def unique_ids(self) -> set:
        return {it[self.id_key] for it in self.pool if self.id_key in it}

    async def __call__(self, user_id, limit, next_page, **kw) -> FeedResult:
        self.calls += 1
        pos = (next_page or {}).get("pos", 0)
        if self.latency:
            await asyncio.sleep(self.latency)
        chunk = self.pool[pos: pos + limit]
        # Fresh copies so downstream in-place stamping never mutates the pool.
        data = [dict(it) for it in chunk]
        for it in data:
            self.emitted.append(it.get(self.id_key))
        new_pos = pos + len(chunk)
        return FeedResult(
            data=data,
            next_page={"pos": new_pos},
            has_next_page=new_pos < len(self.pool),
        )


# ---------------------------------------------------------------------------
# Pool factories (all deterministic -- no RNG, so failures are reproducible)
# ---------------------------------------------------------------------------

def unique_pool(n: int, start: int = 0, val_prefix: str = "u") -> List[Dict]:
    """Ids start..start+n-1, no duplicates. Isolates *item loss* from dedup."""
    return [{"id": start + k, "val": f"{val_prefix}{start + k}"} for k in range(n)]


def dup_pool(n_unique: int, every: int = 4) -> List[Dict]:
    """n_unique distinct ids, with an earlier id re-inserted every ``every`` slots.

    The re-inserted copies are *cross-page* duplicates once paginated. The unique
    universe is still exactly range(n_unique), so ``assert_full_coverage`` derives
    expectations straight from the pool.
    """
    pool: List[Dict] = []
    for k in range(n_unique):
        pool.append({"id": k, "val": f"v{k}"})
        if k >= 2 and k % every == 0:
            dup_id = k - 2
            pool.append({"id": dup_id, "val": f"v{dup_id}"})  # re-emit an earlier id
    return pool


def adjacent_dup_pool(n_unique: int) -> List[Dict]:
    """Each id emitted twice, back-to-back -- both copies land in the same batch,
    so within-batch dedup must catch them (no cross-rebuild ambiguity)."""
    pool: List[Dict] = []
    for k in range(n_unique):
        pool.append({"id": k, "val": f"v{k}"})
        pool.append({"id": k, "val": f"v{k}"})
    return pool


# ---------------------------------------------------------------------------
# Context / node builders
# ---------------------------------------------------------------------------

def make_ctx(methods: Dict, redis=None, session_id: str = "s1") -> ExecutionContext:
    return ExecutionContext(session_id=session_id, methods_dict=methods, redis=redis)


def subfeed(subfeed_id: str, method_name: str, **kw) -> SubFeed:
    return SubFeed(subfeed_id=subfeed_id, method_name=method_name, **kw)


def wrapper(
    child,
    *,
    node_id: str = "w",
    session_size: Optional[int] = None,
    session_ttl: int = 300,
    dedup_key: Optional[str] = None,
    overfetch_factor: int = 4,
    max_refill_loops: int = 2,
    missing_key_policy: str = "error",
    rerank_method: Optional[str] = None,
    rerank_raise: bool = True,
    cache_key: Optional[str] = None,
) -> Wrapper:
    """Build a Wrapper with the requested pipeline stages."""
    cache = None
    if session_size is not None:
        cache = WrapperCache(session_size=session_size, session_ttl=session_ttl)
    dedup = None
    if dedup_key is not None:
        dedup = WrapperDedup(
            dedup_key=dedup_key,
            overfetch_factor=overfetch_factor,
            max_refill_loops=max_refill_loops,
            missing_key_policy=missing_key_policy,
        )
    rerank = None
    if rerank_method is not None:
        rerank = WrapperRerank(method_name=rerank_method, raise_error=rerank_raise)
    return Wrapper(
        node_id=node_id,
        cache=cache,
        dedup=dedup,
        rerank=rerank,
        cache_key=cache_key,
        data=child,
    )


# ---------------------------------------------------------------------------
# Drain + invariant assertions
# ---------------------------------------------------------------------------

async def drain(node, ctx, limit: int, max_pages: int = 500, cursor=None) -> List[List]:
    """Page until ``has_next_page`` is False. Returns the list of page-data lists.

    Raises AssertionError if pagination does not terminate within ``max_pages`` --
    this is the guard that catches infinite-loop bugs (e.g. shared-cache
    continuation re-serving page 1 forever).
    """
    cursor = cursor or {}
    pages: List[List] = []
    for _ in range(max_pages):
        r = await run_executor.run(node, ctx, limit=limit, cursor=cursor)
        pages.append(r.data)
        if not r.has_next_page:
            return pages
        cursor = r.next_page
    raise AssertionError(
        f"pagination did not terminate within {max_pages} pages "
        f"(infinite loop / non-advancing cursor)"
    )


def flat_ids(pages: List[List], key: str = "id") -> List:
    return [it[key] for pg in pages for it in pg if isinstance(it, dict) and key in it]


def assert_no_duplicates(pages: List[List], key: str = "id") -> None:
    ids = flat_ids(pages, key)
    seen, dupes = set(), set()
    for x in ids:
        (dupes if x in seen else seen).add(x)
    assert not dupes, f"{len(dupes)} id(s) served more than once: {sorted(dupes)[:20]}"


def assert_full_coverage(pages: List[List], expected: set, key: str = "id") -> None:
    got = set(flat_ids(pages, key))
    missing = set(expected) - got
    extra = got - set(expected)
    assert not missing, f"{len(missing)} unique id(s) NEVER served (lost): {sorted(missing)[:20]}"
    assert not extra, f"served id(s) outside the source universe: {sorted(extra)[:20]}"


def assert_pages_full(pages: List[List], limit: int) -> None:
    """Every page except the last must be exactly ``limit`` (dedup must refill,
    not short-change, until the source is genuinely exhausted)."""
    for i, pg in enumerate(pages[:-1]):
        assert len(pg) == limit, f"page {i} returned {len(pg)} items, expected {limit}"
