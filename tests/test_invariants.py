"""Property/invariant backbone for dedup + pagination.

Each test drains a FINITE source to exhaustion and asserts one universal
property (coverage / no-duplicates / page-fullness / termination) across the
config matrix, so a single property covers a whole class of configurations.
Uses unique (duplicate-free) pools so coverage/no-duplicate assertions are
unambiguous; duplicate-heavy dedup semantics live in test_dedup_correctness.py.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S


POOL_N = 200


def _redis():
    return fakeredis.aioredis.FakeRedis()


def _build(cfg):
    """cfg keys: cache(bool), dedup(bool), overfetch(int), rerank(bool)."""
    child = S.subfeed("src", "src")
    node = S.wrapper(
        child,
        session_size=50 if cfg["cache"] else None,
        dedup_key="id" if cfg["dedup"] else None,
        overfetch_factor=cfg.get("overfetch", 4),
        rerank_method="identity" if cfg.get("rerank") else None,
    )
    return node


async def identity_rerank(items, session_id):
    return items


def _ctx(src):
    return S.make_ctx({"src": src, "identity": identity_rerank}, redis=_redis())


def _p(id_, marks=(), **cfg):
    return pytest.param(cfg, id=id_, marks=marks)


# Coverage must hold for every overfetch value and both paths: no item is ever lost.
COVERAGE_CONFIGS = [
    _p("cache-nodedup",              cache=True,  dedup=False, overfetch=4),
    _p("passthrough-nodedup",        cache=False, dedup=False, overfetch=4),
    _p("cache-dedup-of1",            cache=True,  dedup=True,  overfetch=1),
    _p("passthrough-dedup-of1",      cache=False, dedup=True,  overfetch=1),
    _p("cache-dedup-of2",            cache=True,  dedup=True,  overfetch=2),
    _p("cache-dedup-of4",            cache=True,  dedup=True,  overfetch=4),
    _p("passthrough-dedup-of2",      cache=False, dedup=True,  overfetch=2),
    _p("passthrough-dedup-of4",      cache=False, dedup=True,  overfetch=4),
    _p("cache-dedup-of4-rerank",     cache=True,  dedup=True,  overfetch=4, rerank=True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", COVERAGE_CONFIGS)
async def test_coverage_no_item_lost(cfg):
    """Draining a finite unique source serves every id exactly once -- nothing lost."""
    src = S.ScriptedSource(S.unique_pool(POOL_N))
    node = _build(cfg)
    pages = await S.drain(node, _ctx(src), limit=10, max_pages=200)
    S.assert_full_coverage(pages, set(range(POOL_N)))


# Same matrix WITHOUT the LOSS marks: item loss shows up as coverage gaps, NOT as
# duplicates or non-termination, so these properties hold for every config.
NODUP_CONFIGS = [
    _p("cache-nodedup",          cache=True,  dedup=False, overfetch=4),
    _p("passthrough-nodedup",    cache=False, dedup=False, overfetch=4),
    _p("cache-dedup-of1",        cache=True,  dedup=True,  overfetch=1),
    _p("passthrough-dedup-of1",  cache=False, dedup=True,  overfetch=1),
    _p("cache-dedup-of2",        cache=True,  dedup=True,  overfetch=2),
    _p("cache-dedup-of4",        cache=True,  dedup=True,  overfetch=4),
    _p("passthrough-dedup-of2",  cache=False, dedup=True,  overfetch=2),
    _p("passthrough-dedup-of4",  cache=False, dedup=True,  overfetch=4),
    _p("cache-dedup-of4-rerank", cache=True,  dedup=True,  overfetch=4, rerank=True),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", NODUP_CONFIGS)
async def test_no_duplicates_across_pages(cfg):
    """No id is ever served twice (unique source; loss must not manifest as dupes)."""
    src = S.ScriptedSource(S.unique_pool(POOL_N))
    node = _build(cfg)
    pages = await S.drain(node, _ctx(src), limit=10, max_pages=200)
    S.assert_no_duplicates(pages)


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", [
    _p("cache-nodedup",         cache=True,  dedup=False, overfetch=4),
    _p("passthrough-nodedup",   cache=False, dedup=False, overfetch=4),
    _p("cache-dedup-of1",       cache=True,  dedup=True,  overfetch=1),
    _p("cache-dedup-of4",       cache=True,  dedup=True,  overfetch=4),
    _p("passthrough-dedup-of4", cache=False, dedup=True,  overfetch=4),
])
async def test_pages_are_full_until_exhaustion(cfg):
    """Every non-final page has exactly `limit` items (loss does not shorten pages,
    so this passes even where coverage fails -- keeping the two concerns separate)."""
    src = S.ScriptedSource(S.unique_pool(POOL_N))
    node = _build(cfg)
    pages = await S.drain(node, _ctx(src), limit=10, max_pages=200)
    S.assert_pages_full(pages, limit=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("cfg", NODUP_CONFIGS)
async def test_pagination_terminates(cfg):
    """A finite source must produce a terminating pagination (has_next -> False)."""
    src = S.ScriptedSource(S.unique_pool(POOL_N))
    node = _build(cfg)
    # drain raises if it does not terminate; reaching this line means it did.
    await S.drain(node, _ctx(src), limit=10, max_pages=200)
