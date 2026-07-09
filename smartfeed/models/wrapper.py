from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Set, Tuple

import orjson
from pydantic import BaseModel, PrivateAttr, model_validator

from smartfeed.execution import executor as _executor

from .base import BaseNode, FeedResult, coerce_feed_node

if TYPE_CHECKING:
    from smartfeed.execution.context import ExecutionContext

# Safety bound for the dedup refill loop: stop scanning after this many fetched items
# per `target` when the child keeps yielding duplicates (guards against a misbehaving
# source that returns has_next=True forever). Well above any realistic duplicate rate.
_REFILL_SCAN_FACTOR = 50

# Cold-build lock: how long a builder may hold the lock, and how a loser waits it out.
# The loser's poll budget covers the FULL lock TTL -- giving up earlier and rebuilding
# without the lock would double-fetch the child and write a competing generation.
_COLD_LOCK_TTL = 10
_LOCK_POLL_INTERVAL = 0.1
_LOCK_POLL_ROUNDS = int(_COLD_LOCK_TTL / _LOCK_POLL_INTERVAL)


class WrapperCache(BaseModel):
    session_size: int = 300
    session_ttl: int = 300


class WrapperRerank(BaseModel):
    method_name: str
    raise_error: bool = True  # True = crash if rerank fails, False = keep original order


class WrapperDedup(BaseModel):
    dedup_key: str
    missing_key_policy: Literal["error", "keep", "drop"] = "error"
    state_ttl: int = 300  # TTL for Redis seen-set (seconds)


class Wrapper(BaseNode):
    type: Literal["wrapper"] = "wrapper"
    node_id: str
    cache: Optional[WrapperCache] = None
    rerank: Optional[WrapperRerank] = None
    dedup: Optional[WrapperDedup] = None
    data: Any  # BaseNode subclass; typed as Any to support discriminated union deserialization
    cache_key: Optional[str] = None

    # Config is immutable after validation, so the priority map is computed once.
    _priorities: Optional[Dict[str, int]] = PrivateAttr(default=None)

    @model_validator(mode="before")
    @classmethod
    def _coerce_data(cls, values: Any) -> Any:
        if isinstance(values, dict) and isinstance(values.get("data"), dict):
            values["data"] = coerce_feed_node(values["data"])
        return values

    async def execute(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        cursor: Dict[str, Any],
        ctx: Optional[ExecutionContext] = None,
        **params: Any,
    ) -> FeedResult:
        """Entry point. Routes to cached or passthrough path."""
        if self.cache is None or ctx is None or ctx.redis is None:
            return await self._passthrough(methods_dict, session_id, limit, cursor, ctx)

        return await self._execute_with_cache(methods_dict, session_id, limit, cursor, ctx)

    # -- dedup -----------------------------------------------------------------

    def _resolve_dedup_priorities(self) -> Dict[str, int]:
        """Build {subfeed_id: effective_priority} by walking the config tree.

        The config never changes after validation, so the walk runs once per
        wrapper instance instead of once per dedup round."""
        if self._priorities is None:
            result: Dict[str, int] = {}
            self._collect_priorities(self.data, override_priority=0, out=result)
            self._priorities = result
        return self._priorities

    @staticmethod
    def _collect_priorities(node: BaseNode, override_priority: int, out: Dict[str, int]) -> None:
        """Recursive walk: propagate override_priority down, write SubFeed priorities to out."""
        from .subfeed import SubFeed

        # Determine the effective priority for this subtree.
        # A node with dedup_priority != 0 overrides all children.
        effective = node.dedup_priority if node.dedup_priority != 0 else override_priority

        if isinstance(node, SubFeed):
            out[node.subfeed_id] = effective
            return

        # Walk children: look in known child-bearing attributes.
        # The BaseNode check comes BEFORE the .data unwrap: a Wrapper is the one
        # node that ALSO has .data, and it must be visited itself so its own
        # dedup_priority participates in propagation.
        for attr in ("items", "data", "positional", "default", "item_from", "item_to"):
            child = getattr(node, attr, None)
            if child is None:
                continue
            children = child if isinstance(child, list) else [child]
            for item in children:
                if isinstance(item, BaseNode):
                    Wrapper._collect_priorities(item, effective, out)
                elif isinstance(item, BaseModel):
                    # MergerPercentageItem and friends wrap their node in .data
                    inner = getattr(item, "data", None)
                    if isinstance(inner, BaseNode):
                        Wrapper._collect_priorities(inner, effective, out)

    def _dedup(self, data: List[Any], seen: Optional[Set[str]] = None) -> List[Any]:
        """Remove duplicates by dedup_key. Higher dedup_priority wins, equal = first-seen wins."""
        if not self.dedup:
            return data

        priorities = self._resolve_dedup_priorities()
        key_field = self.dedup.dedup_key
        policy = self.dedup.missing_key_policy
        if seen is None:
            seen = set()

        # {key_val: (priority, index_in_result)} for in-batch priority arbitration
        batch: Dict[str, Tuple[int, int]] = {}
        result: List[Any] = []

        for item in data:
            if not isinstance(item, dict):
                result.append(item)
                continue

            if key_field not in item:
                if policy == "error":
                    raise KeyError(f"Dedup key '{key_field}' missing from item: {item}")
                elif policy == "keep":
                    result.append(item)
                elif policy == "drop":
                    pass
                continue

            key_val = str(item[key_field])

            # Cross-page / cross-rebuild: already shown earlier in this scroll.
            # `key_val in batch` means it is a *within-batch* duplicate, which must
            # fall through to priority arbitration below rather than be skipped here.
            if key_val in seen and key_val not in batch:
                continue

            # In-batch priority arbitration
            source = (item.get("_smartfeed_debug_info") or {}).get("source")
            item_priority = priorities.get(source, 0) if source else 0

            if key_val not in batch:
                idx = len(result)
                batch[key_val] = (item_priority, idx)
                result.append(item)
            else:
                existing_priority, existing_idx = batch[key_val]
                if item_priority > existing_priority:
                    result[existing_idx] = item
                    batch[key_val] = (item_priority, existing_idx)

            seen.add(key_val)

        return result

    # -- rerank ----------------------------------------------------------------

    async def _apply_rerank(self, data: List[Any], methods_dict: Dict[str, Any], session_id: str) -> List[Any]:
        """Call rerank callable from methods_dict. Validates output length."""
        if not self.rerank:
            return data
        rerank_fn = methods_dict[self.rerank.method_name]
        original_len = len(data)
        try:
            reranked: List[Any] = await rerank_fn(data, session_id)
        except Exception:
            if self.rerank.raise_error:
                raise
            # Fail-soft: keep original order
            return data
        if len(reranked) != original_len:
            raise ValueError(
                f"Rerank '{self.rerank.method_name}' must return exactly " f"{original_len} items, got {len(reranked)}"
            )
        return reranked

    # -- debug stamping --------------------------------------------------------

    def _stamp_pre_rerank(self, data: List[Any]) -> None:
        """Stamp smartfeed_position on each item (position before rerank)."""
        for i, item in enumerate(data):
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {})[self.node_id] = {
                    "smartfeed_position": i,
                }

    def _stamp_post_rerank(self, data: List[Any]) -> None:
        """Stamp rerank_position on each item (position after rerank)."""
        for i, item in enumerate(data):
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {}).setdefault(self.node_id, {})["rerank_position"] = i

    # -- passthrough (no cache) -----------------------------------------------

    async def _passthrough(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        cursor: Dict[str, Any],
        ctx: Optional[ExecutionContext],
    ) -> FeedResult:
        """No-cache path: fetch -> dedup (refill to a full page) -> rerank -> return."""
        assert ctx is not None, "wrapper execution requires ctx (the executor always provides it)"
        if self.dedup:
            # Session-scoped seen-set, mirroring the cached path: an empty cursor is a
            # fresh scroll, so reset the set (re-requesting page 1 returns page 1);
            # otherwise load it so shown ids persist across pages.
            if not cursor:
                await self._reset_seen_set(ctx, session_id)
                seen_keys: Set[str] = set()
            else:
                seen_keys = await self._load_seen_set(ctx, session_id)

            current_cursor = dict(cursor)
            data: List[Any] = []
            has_next = True
            scanned = 0
            scan_cap = max(limit, 1) * _REFILL_SCAN_FACTOR

            # Passthrough has no buffer, so it must never over-fetch: pull exactly the
            # outstanding deficit each round and refill until the page is full or the
            # child is exhausted. Nothing is discarded, so nothing is lost. Keep scanning
            # through duplicate runs (don't stop on a single all-duplicate window), bounded
            # by scan_cap so a duplicate-only source can't spin forever.
            while len(data) < limit and has_next and scanned < scan_cap:
                need = limit - len(data)
                result = await _executor.run(self.data, ctx, need, current_cursor)
                current_cursor = result.next_page
                has_next = result.has_next_page
                if not result.data:
                    break  # child yielded nothing -> stop instead of spinning
                scanned += len(result.data)
                data.extend(self._dedup(result.data, seen_keys))

            # Persist seen-set to Redis for the next page.
            new_keys: Set[str] = set()
            key_field = self.dedup.dedup_key
            for item in data:
                if isinstance(item, dict) and key_field in item:
                    new_keys.add(str(item[key_field]))
            await self._save_seen_set(ctx, session_id, new_keys)
        else:
            result = await _executor.run(self.data, ctx, limit, cursor)
            data = result.data
            current_cursor = result.next_page
            has_next = result.has_next_page

        self._stamp_pre_rerank(data)
        if self.rerank:
            data = await self._apply_rerank(data, methods_dict, session_id)
            self._stamp_post_rerank(data)
        return FeedResult(
            data=data,
            next_page=current_cursor,
            has_next_page=has_next,
        )

    def _seen_set_key(self, session_id: str) -> str:
        """Redis key for cross-page dedup seen-set."""
        return f"sf:{session_id}:{self.node_id}:{self.config_hash()}:seen"

    async def _load_seen_set(self, ctx: Optional[ExecutionContext], session_id: str) -> Set[str]:
        """Load seen keys from Redis SET for cross-page dedup."""
        if not ctx or not ctx.redis:
            return set()
        key = self._seen_set_key(session_id)
        members = await ctx.redis.smembers(key)
        return {m.decode() if isinstance(m, bytes) else m for m in members} if members else set()

    async def _save_seen_set(self, ctx: Optional[ExecutionContext], session_id: str, new_keys: Set[str]) -> None:
        """Append new keys to Redis seen-set and refresh TTL (one round-trip)."""
        if not ctx or not ctx.redis or not new_keys:
            return
        key = self._seen_set_key(session_id)
        assert self.dedup is not None, "seen-set only written when dedup configured"
        pipe = ctx.redis.pipeline()
        pipe.sadd(key, *new_keys)
        pipe.expire(key, self.dedup.state_ttl)
        await pipe.execute()

    async def _reset_seen_set(self, ctx: Optional[ExecutionContext], session_id: str) -> None:
        """Clear the seen-set so a fresh scroll (empty cursor) starts from page 1."""
        if not ctx or not ctx.redis:
            return
        await ctx.redis.delete(self._seen_set_key(session_id))

    # -- cache helpers ---------------------------------------------------------
    #
    # The session batch is stored as a Redis LIST of individually-encoded items, so a
    # warm page read transfers and parses only the requested window (LRANGE), never
    # the whole batch. Shared-base segments stay single blobs: they are always read
    # whole (each sharing wrapper reranks the full segment).

    def _base_key(self, session_id: str) -> str:
        """Redis key prefix for this wrapper's cache data."""
        key_part = self.cache_key or self.node_id
        return f"sf:{session_id}:{key_part}:{self.config_hash()}"

    async def _read_meta(self, ctx: ExecutionContext, session_id: str) -> Optional[Dict[str, Any]]:
        """Read cache metadata (gen, child_cursor, child_has_next) from Redis."""
        assert ctx.redis is not None, "cached path requires redis"
        base = self._base_key(session_id)
        raw = await ctx.redis.get(f"{base}:meta")
        if raw is None:
            return None
        meta: Dict[str, Any] = orjson.loads(raw)
        return meta

    async def _write_cache(
        self,
        ctx: ExecutionContext,
        session_id: str,
        data: List[Any],
        gen: str,
        child_cursor: Dict[str, Any],
        child_has_next: bool = False,
    ) -> None:
        """Replace session data (as a LIST of encoded items) and metadata atomically."""
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        assert ctx.redis is not None, "cached path requires redis"
        base = self._base_key(session_id)
        ttl = self.cache.session_ttl

        try:
            encoded = [orjson.dumps(item) for item in data]
            meta = orjson.dumps({"gen": gen, "child_cursor": child_cursor, "child_has_next": child_has_next})
        except TypeError as e:
            raise TypeError(
                f"Wrapper '{self.node_id}': cache is enabled, so items and child cursors must be "
                f"orjson-serializable (str/int/float/bool/None/list/dict): {e}"
            ) from e
        pipe = ctx.redis.pipeline()
        pipe.delete(base)
        if encoded:
            pipe.rpush(base, *encoded)
            pipe.expire(base, ttl)
        pipe.set(f"{base}:meta", meta, ex=ttl)
        await pipe.execute()

    async def _read_snapshot_and_touch(
        self, ctx: ExecutionContext, session_id: str, offset: int, limit: int
    ) -> Tuple[Optional[Dict[str, Any]], List[Any], int]:
        """Read meta + one page window as ONE atomic snapshot and refresh TTLs.

        A single MULTI/EXEC round-trip: reading meta and data as separate commands
        would let a concurrent rebuild land in between, pairing old-gen meta with the
        replacement batch (torn read) -- `_write_cache` swaps both keys in one
        transaction, so reads go through one transaction too. Only the requested
        window is transferred and parsed (LRANGE), not the whole session batch. The
        TTL refresh rides along: data/meta get session_ttl and the dedup seen-set gets
        state_ttl, so dedup state cannot expire mid-scroll while the cache it guards
        is being kept alive.

        Returns (meta, page_items, total_batch_len); meta is None on miss.
        """
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        assert ctx.redis is not None, "cached path requires redis"
        base = self._base_key(session_id)
        ttl = self.cache.session_ttl
        pipe = ctx.redis.pipeline()
        pipe.get(f"{base}:meta")
        pipe.llen(base)
        pipe.lrange(base, offset, offset + limit - 1)
        pipe.expire(base, ttl)
        pipe.expire(f"{base}:meta", ttl)
        if self.dedup:
            pipe.expire(self._seen_set_key(session_id), self.dedup.state_ttl)
        results = await pipe.execute()
        raw_meta, total, raw_page = results[0], results[1], results[2]
        meta: Optional[Dict[str, Any]] = orjson.loads(raw_meta) if raw_meta is not None else None
        page = [orjson.loads(x) for x in raw_page]
        return meta, page, int(total)

    async def _serve_new_generation(
        self, ctx: ExecutionContext, session_id: str, limit: int, stale_gen: Optional[str]
    ) -> Optional[FeedResult]:
        """Serve page 1 of the current batch, but only if it is a NEW generation.

        Used by cold-build waiters: the outgoing batch's keys are never deleted before
        a rebuild, so a batch still carrying `stale_gen` is the one being replaced --
        serving it would re-show items and hand back an instantly-stale cursor.
        """
        meta, page, total = await self._read_snapshot_and_touch(ctx, session_id, 0, limit)
        if meta is None or meta.get("gen") == stale_gen:
            return None
        child_has_next = meta.get("child_has_next", bool(meta.get("child_cursor")))
        return self._page_result(page, 0, limit, total, meta.get("gen", ""), child_has_next)

    # -- pagination ------------------------------------------------------------

    def _page_result(
        self,
        page_data: List[Any],
        offset: int,
        limit: int,
        total: int,
        gen: str,
        child_has_next: bool = False,
    ) -> FeedResult:
        """Build a page response from an already-sliced window of the session batch.

        The cursor carries an absolute offset (not a page number), so pagination stays
        correct even if the client varies `limit` between requests.
        """
        end = offset + limit
        has_next = end < total or child_has_next

        next_cursor = {
            self.node_id: {
                "offset": end,
                "gen": gen,
            }
        }

        return FeedResult(
            data=page_data,
            next_page=next_cursor,
            has_next_page=has_next,
        )

    # -- main cached flow ------------------------------------------------------

    async def _execute_with_cache(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        cursor: Dict[str, Any],
        ctx: ExecutionContext,
    ) -> FeedResult:
        """Cached path: warm hit -> paginate, stale/miss -> cold build."""
        # Cursors round-trip through untrusted clients: reject malformed contents
        # loudly instead of crashing mid-slice or serving a wrong page.
        my_cursor = cursor.get(self.node_id, {})
        if not isinstance(my_cursor, dict):
            raise ValueError(
                f"Wrapper '{self.node_id}': cursor entry must be a dict, "
                f"got {type(my_cursor).__name__} (pass back next_page verbatim)"
            )
        cursor_gen = my_cursor.get("gen")
        if cursor_gen is not None and not isinstance(cursor_gen, str):
            raise ValueError(f"Wrapper '{self.node_id}': cursor 'gen' must be a string, got {cursor_gen!r}")
        cursor_offset = my_cursor.get("offset", 0)
        if not isinstance(cursor_offset, int) or isinstance(cursor_offset, bool) or cursor_offset < 0:
            raise ValueError(
                f"Wrapper '{self.node_id}': cursor 'offset' must be a non-negative int, got {cursor_offset!r}"
            )

        # Warm path: try to read existing cache
        if cursor_gen:
            meta, page, total = await self._read_snapshot_and_touch(ctx, session_id, cursor_offset, limit)
            if meta and meta.get("gen") == cursor_gen:
                # Serve from cache while this offset is still within the batch.
                if cursor_offset < total:
                    child_has_next = meta.get("child_has_next", bool(meta.get("child_cursor")))
                    return self._page_result(page, cursor_offset, limit, total, cursor_gen, child_has_next)
                # Cache exhausted: rebuild with continuation cursor
                return await self._cold_build_locked(
                    methods_dict,
                    session_id,
                    limit,
                    ctx,
                    child_cursor=meta.get("child_cursor", {}),
                    stale_gen=cursor_gen,
                )
            # Stale gen or evicted cache -> fresh build replacing whatever batch exists now.
            return await self._cold_build_locked(
                methods_dict,
                session_id,
                limit,
                ctx,
                child_cursor={},
                stale_gen=meta.get("gen") if meta else None,
            )

        # Fresh scroll (no gen): rebuild, replacing the current batch if one exists.
        # Its gen is the baseline a lock waiter must NOT accept as the new build.
        meta = await self._read_meta(ctx, session_id)
        return await self._cold_build_locked(
            methods_dict,
            session_id,
            limit,
            ctx,
            child_cursor={},
            stale_gen=meta.get("gen") if meta else None,
        )

    async def _cold_build_locked(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        ctx: ExecutionContext,
        child_cursor: Dict[str, Any],
        stale_gen: Optional[str] = None,
    ) -> FeedResult:
        """Cold build guarded by a lock so concurrent rebuilds fetch the child ONCE:
        one caller builds and writes the cache, the rest wait for it and serve the
        new batch from offset 0.

        `stale_gen` is the generation being replaced (None if none existed). The old
        keys are not deleted before a rebuild, so a waiter must ignore metas still
        carrying `stale_gen`: accepting the first meta it sees would re-serve the
        outgoing batch and hand back a cursor that is stale the moment the builder
        finishes. Waiters poll for the full lock TTL (the builder can hold it no
        longer); if no new generation appears by then the builder is dead, so the
        second round retries the lock, and only after that do we build unlocked as
        a last resort.
        """
        from smartfeed.execution.redis_lock import RedisLock

        assert ctx.redis is not None, "cached path requires redis"
        lock_key = f"{self._base_key(session_id)}:coldlock"
        for _ in range(2):
            async with RedisLock(ctx.redis, lock_key, ttl=_COLD_LOCK_TTL) as acquired:
                if acquired:
                    # Another caller may have finished this same rebuild while we
                    # queued for the lock -- serve its batch instead of re-fetching.
                    meta = await self._read_meta(ctx, session_id)
                    if meta and meta.get("gen") != stale_gen:
                        served = await self._serve_new_generation(ctx, session_id, limit, stale_gen)
                        if served is not None:
                            return served
                    return await self._cold_build(methods_dict, session_id, limit, ctx, child_cursor)
            # Another coroutine is building -- wait for it to publish a NEW generation.
            for _ in range(_LOCK_POLL_ROUNDS):
                await asyncio.sleep(_LOCK_POLL_INTERVAL)
                meta = await self._read_meta(ctx, session_id)
                if meta and meta.get("gen") != stale_gen:
                    served = await self._serve_new_generation(ctx, session_id, limit, stale_gen)
                    if served is not None:
                        return served
        # Two rounds without acquiring or seeing a new generation: build unlocked so
        # the request still completes.
        return await self._cold_build(methods_dict, session_id, limit, ctx, child_cursor)

    # -- shared base cache helpers ----------------------------------------------

    @staticmethod
    def _cursor_segment(child_cursor: Dict[str, Any]) -> str:
        """Stable short tag for a continuation position, so each page-window of the
        shared base is its own segment (page 1 = "0")."""
        if not child_cursor:
            return "0"
        raw = json.dumps(child_cursor, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def _base_shared_key(self, session_id: str, child_cursor: Dict[str, Any]) -> str:
        """Key for a shared-base segment (deduped, NOT reranked), keyed by cache_key
        and the continuation position so continuations don't collide with page 1."""
        seg = self._cursor_segment(child_cursor)
        return f"sf:{session_id}:{self.cache_key}:{self.data.config_hash()}:{seg}"

    async def _read_shared_base(
        self, ctx: ExecutionContext, session_id: str, child_cursor: Dict[str, Any]
    ) -> Optional[List[Any]]:
        """Read a shared-base segment from Redis. Returns None on miss."""
        assert ctx.redis is not None, "cached path requires redis"
        key = self._base_shared_key(session_id, child_cursor)
        raw = await ctx.redis.get(key)
        if raw is None:
            return None
        segment: List[Any] = orjson.loads(raw)
        return segment

    async def _write_shared_base(
        self,
        ctx: ExecutionContext,
        session_id: str,
        child_cursor: Dict[str, Any],
        data: List[Any],
        child_cursor_out: Dict[str, Any],
        child_has_next: bool = False,
    ) -> None:
        """Write a shared-base segment and its meta to Redis with TTL."""
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        assert ctx.redis is not None, "cached path requires redis"
        key = self._base_shared_key(session_id, child_cursor)
        ttl = self.cache.session_ttl
        try:
            payload = orjson.dumps(data)
            meta = orjson.dumps({"child_cursor": child_cursor_out, "child_has_next": child_has_next})
        except TypeError as e:
            raise TypeError(
                f"Wrapper '{self.node_id}': cache is enabled, so items and child cursors must be "
                f"orjson-serializable (str/int/float/bool/None/list/dict): {e}"
            ) from e
        pipe = ctx.redis.pipeline()
        pipe.set(key, payload, ex=ttl)
        pipe.set(f"{key}:meta", meta, ex=ttl)
        await pipe.execute()

    async def _read_shared_base_meta(
        self, ctx: ExecutionContext, session_id: str, child_cursor: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Read a shared-base segment's metadata (child_cursor, child_has_next)."""
        assert ctx.redis is not None, "cached path requires redis"
        key = self._base_shared_key(session_id, child_cursor)
        raw = await ctx.redis.get(f"{key}:meta")
        if raw is None:
            return None
        meta: Dict[str, Any] = orjson.loads(raw)
        return meta

    async def _fetch_and_dedup(
        self,
        ctx: ExecutionContext,
        target: int,
        child_cursor: Dict[str, Any],
        seen: Optional[Set[str]] = None,
    ) -> Tuple[List[Any], Dict[str, Any], bool]:
        """Collect up to `target` deduped items from the child.

        Fetches exactly the outstanding deficit each round and keeps EVERY survivor
        (never truncates, never advances the child cursor past an unconsumed unique),
        so no item is lost. Refills until `target` unique items are collected or the
        child is exhausted, so pages fill under dedup attrition instead of coming up
        short. `seen`, if given, holds keys already shown earlier in this scroll (so
        they are not repeated across a rebuild boundary); `_dedup` mutates it in place
        with the keys emitted here.

        Returns (data, child_cursor, child_has_next).
        """
        cursor = dict(child_cursor)
        data: List[Any] = []
        # One seen-set across ALL refill rounds: a fresh set per round would re-admit
        # round-1 ids, letting duplicates survive within one build.
        if seen is None:
            seen = set()
        has_next = True
        scanned = 0
        scan_cap = max(target, 1) * _REFILL_SCAN_FACTOR

        # Keep scanning through duplicate runs until `target` unique items are
        # collected or the child is exhausted; bounded by scan_cap so a
        # duplicate-only source can't spin forever.
        while len(data) < target and has_next and scanned < scan_cap:
            need = target - len(data)
            result = await _executor.run(self.data, ctx, need, cursor)
            cursor = result.next_page
            has_next = result.has_next_page
            if not result.data:
                break  # child yielded nothing -> stop instead of spinning
            scanned += len(result.data)
            new = self._dedup(result.data, seen) if self.dedup else result.data
            data.extend(new)

        return data, cursor, has_next

    async def _cold_build(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        ctx: ExecutionContext,
        child_cursor: Dict[str, Any],
    ) -> FeedResult:
        """Build full session: fetch -> dedup -> rerank -> write cache -> paginate page 1."""
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        session_size = self.cache.session_size

        if self.cache_key is not None:
            # Shared cache path: base data (fetch + dedup) is shared across wrappers
            # with the same cache_key; rerank is applied per-wrapper after. Each
            # continuation position is a distinct shared segment (see _base_shared_key).
            base_data = await self._build_shared_base(methods_dict, session_id, ctx, child_cursor)
            shared_meta = await self._read_shared_base_meta(ctx, session_id, child_cursor)
            child_cursor_out = shared_meta.get("child_cursor", {}) if shared_meta else {}
            child_has_next = shared_meta.get("child_has_next", False) if shared_meta else False
            # Per-wrapper: stamp pre-rerank, apply rerank, stamp post-rerank
            data = list(base_data)
            self._stamp_pre_rerank(data)
            data = await self._apply_rerank(data, methods_dict, session_id)
            if self.rerank:
                self._stamp_post_rerank(data)
        else:
            # Standard path. Session-scoped seen-set: reset on a fresh scroll (empty
            # cursor) so re-requesting page 1 returns page 1, and carry it across
            # rebuilds so a shown id never repeats later in the same scroll.
            seen: Optional[Set[str]] = None
            preloaded: Set[str] = set()
            if self.dedup:
                if not child_cursor:
                    await self._reset_seen_set(ctx, session_id)
                    seen = set()
                else:
                    seen = await self._load_seen_set(ctx, session_id)
                    preloaded = set(seen)
            data, child_cursor_out, child_has_next = await self._fetch_and_dedup(
                ctx, session_size, child_cursor, seen=seen
            )
            if self.dedup:
                assert seen is not None
                # SADD only the delta: everything in `preloaded` is already in Redis.
                await self._save_seen_set(ctx, session_id, seen - preloaded)
            self._stamp_pre_rerank(data)
            data = await self._apply_rerank(data, methods_dict, session_id)
            if self.rerank:
                self._stamp_post_rerank(data)

        gen = secrets.token_hex(4)
        await self._write_cache(ctx, session_id, data, gen, child_cursor_out, child_has_next)
        return self._page_result(data[:limit], 0, limit, len(data), gen, child_has_next)

    async def _build_shared_base(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        ctx: ExecutionContext,
        child_cursor: Dict[str, Any],
    ) -> List[Any]:
        """Fetch and dedup the shared base data, using a distributed lock to ensure
        only one caller fetches from child on a cold build.  Others wait and read."""
        from smartfeed.execution.redis_lock import RedisLock

        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        assert ctx.redis is not None, "cached path requires redis"
        session_size = self.cache.session_size
        lock_key = f"{self._base_shared_key(session_id, child_cursor)}:lock"

        # Fast path: this segment already cached
        existing = await self._read_shared_base(ctx, session_id, child_cursor)
        if existing is not None:
            return existing

        # Segments are only ever created (never rebuilt in place), so "segment exists"
        # is the completion signal. Waiters poll for the full lock TTL; if the segment
        # never appears the builder is dead, so the second round retries the lock, and
        # only after that do we fetch unlocked as a last resort.
        for _ in range(2):
            async with RedisLock(ctx.redis, lock_key, ttl=_COLD_LOCK_TTL) as acquired:
                if acquired:
                    # Double-check after acquiring lock
                    existing = await self._read_shared_base(ctx, session_id, child_cursor)
                    if existing is not None:
                        return existing

                    base_data, child_cursor_out, child_has_next = await self._fetch_and_dedup(
                        ctx, session_size, child_cursor
                    )
                    await self._write_shared_base(
                        ctx, session_id, child_cursor, base_data, child_cursor_out, child_has_next
                    )
                    return base_data
            # Another coroutine holds the lock -- poll until the segment appears.
            for _ in range(_LOCK_POLL_ROUNDS):
                await asyncio.sleep(_LOCK_POLL_INTERVAL)
                existing = await self._read_shared_base(ctx, session_id, child_cursor)
                if existing is not None:
                    return existing
        # Two rounds without acquiring or seeing the segment: fetch unlocked so the
        # request still completes.
        data, _, _ = await self._fetch_and_dedup(ctx, session_size, child_cursor)
        return data
