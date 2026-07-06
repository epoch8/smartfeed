from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, model_validator

import orjson
from smartfeed.execution import executor as _executor
from .base import BaseNode, FeedResult, coerce_feed_node

# Safety bound for the dedup refill loop: stop scanning after this many fetched items
# per `target` when the child keeps yielding duplicates (guards against a misbehaving
# source that returns has_next=True forever). Well above any realistic duplicate rate.
_REFILL_SCAN_FACTOR = 50


class WrapperCache(BaseModel):
    session_size: int = 300
    session_ttl: int = 300


class WrapperRerank(BaseModel):
    method_name: str
    raise_error: bool = True  # True = crash if rerank fails, False = keep original order


class WrapperDedup(BaseModel):
    dedup_key: str
    missing_key_policy: Literal["error", "keep", "drop"] = "error"
    # Deprecated / no effect: the dedup fetch now pulls exactly the outstanding
    # deficit and refills until the target is filled or the child is exhausted, so
    # it neither over-fetches (no item loss) nor gives up early (no short pages).
    # Kept for config back-compat.
    overfetch_factor: int = 4
    max_refill_loops: int = 2
    state_ttl: int = 300  # TTL for Redis seen-set (seconds)


class Wrapper(BaseNode):
    type: Literal["wrapper"] = "wrapper"
    node_id: str
    cache: Optional[WrapperCache] = None
    rerank: Optional[WrapperRerank] = None
    dedup: Optional[WrapperDedup] = None
    data: Any  # BaseNode subclass; typed as Any to support discriminated union deserialization
    cache_key: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_data(cls, values: Any) -> Any:
        if isinstance(values, dict) and isinstance(values.get("data"), dict):
            values["data"] = coerce_feed_node(values["data"])
        return values

    async def execute(
        self,
        methods_dict: dict,
        session_id: str,
        limit: int,
        cursor: dict,
        ctx: Any = None,
        **params: Any,
    ) -> FeedResult:
        """Entry point. Routes to cached or passthrough path."""
        if self.cache is None or ctx is None or ctx.redis is None:
            return await self._passthrough(methods_dict, session_id, limit, cursor, ctx)

        return await self._execute_with_cache(methods_dict, session_id, limit, cursor, ctx)

    # -- dedup -----------------------------------------------------------------

    def _resolve_dedup_priorities(self) -> Dict[str, int]:
        """Build {subfeed_id: effective_priority} by walking the config tree."""
        result: Dict[str, int] = {}
        self._collect_priorities(self.data, override_priority=0, out=result)
        return result

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

        # Walk children: look in known child-bearing attributes
        for attr in ("items", "data", "positional", "default", "item_from", "item_to"):
            child = getattr(node, attr, None)
            if child is None:
                continue
            if isinstance(child, list):
                for item in child:
                    child_node = getattr(item, "data", item)
                    if isinstance(child_node, BaseNode):
                        Wrapper._collect_priorities(child_node, effective, out)
            elif isinstance(child, BaseModel):
                # MergerPercentageItem has .data
                child_node = getattr(child, "data", child)
                if isinstance(child_node, BaseNode):
                    Wrapper._collect_priorities(child_node, effective, out)
            elif isinstance(child, BaseNode):
                Wrapper._collect_priorities(child, effective, out)

    def _dedup(self, data: List, seen: Optional[set] = None) -> List:
        """Remove duplicates by dedup_key. Higher dedup_priority wins, equal = first-seen wins."""
        if not self.dedup:
            return data

        priorities = self._resolve_dedup_priorities()
        key_field = self.dedup.dedup_key
        policy = self.dedup.missing_key_policy
        if seen is None:
            seen = set()

        # {key_val: (priority, index_in_result)} for in-batch priority arbitration
        batch: Dict[Any, tuple] = {}
        result: List = []

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

    async def _apply_rerank(self, data: list, methods_dict: dict, session_id: str) -> list:
        """Call rerank callable from methods_dict. Validates output length."""
        if not self.rerank:
            return data
        rerank_fn = methods_dict[self.rerank.method_name]
        original_len = len(data)
        try:
            result = await rerank_fn(data, session_id)
        except Exception:
            if self.rerank.raise_error:
                raise
            # Fail-soft: keep original order
            return data
        if len(result) != original_len:
            raise ValueError(
                f"Rerank '{self.rerank.method_name}' must return exactly " f"{original_len} items, got {len(result)}"
            )
        return result

    # -- debug stamping --------------------------------------------------------

    def _stamp_pre_rerank(self, data: list) -> None:
        """Stamp smartfeed_position on each item (position before rerank)."""
        for i, item in enumerate(data):
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {})[self.node_id] = {
                    "smartfeed_position": i,
                }

    def _stamp_post_rerank(self, data: list) -> None:
        """Stamp rerank_position on each item (position after rerank)."""
        for i, item in enumerate(data):
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {}).setdefault(self.node_id, {})["rerank_position"] = i

    # -- passthrough (no cache) -----------------------------------------------

    async def _passthrough(
        self,
        methods_dict: dict,
        session_id: str,
        limit: int,
        cursor: dict,
        ctx: Any,
    ) -> FeedResult:
        """No-cache path: fetch -> dedup (refill to a full page) -> rerank -> return."""
        if self.dedup:
            # Cross-page seen-set from Redis (persists shown ids across pages).
            seen_keys = await self._load_seen_set(ctx, session_id)

            current_cursor = dict(cursor)
            data: List = []
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
            new_keys = set()
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

    async def _load_seen_set(self, ctx: Any, session_id: str) -> set:
        """Load seen keys from Redis SET for cross-page dedup."""
        if not ctx or not ctx.redis:
            return set()
        key = self._seen_set_key(session_id)
        members = await ctx.redis.smembers(key)
        return {m.decode() if isinstance(m, bytes) else m for m in members} if members else set()

    async def _save_seen_set(self, ctx: Any, session_id: str, new_keys: set) -> None:
        """Append new keys to Redis seen-set and refresh TTL."""
        if not ctx or not ctx.redis or not new_keys:
            return
        key = self._seen_set_key(session_id)
        await ctx.redis.sadd(key, *new_keys)
        assert self.dedup is not None, "seen-set only written when dedup configured"
        ttl = self.dedup.state_ttl
        await ctx.redis.expire(key, ttl)

    async def _reset_seen_set(self, ctx: Any, session_id: str) -> None:
        """Clear the seen-set so a fresh scroll (empty cursor) starts from page 1."""
        if not ctx or not ctx.redis:
            return
        await ctx.redis.delete(self._seen_set_key(session_id))

    # -- cache helpers ---------------------------------------------------------

    def _base_key(self, session_id: str) -> str:
        """Redis key prefix for this wrapper's cache data."""
        key_part = self.cache_key or self.node_id
        return f"sf:{session_id}:{key_part}:{self.config_hash()}"

    async def _read_cache(self, ctx: Any, session_id: str) -> Optional[List]:
        """Read cached session data from Redis. Returns None on miss."""
        base = self._base_key(session_id)
        raw = await ctx.redis.get(base)
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _read_meta(self, ctx: Any, session_id: str) -> Optional[Dict]:
        """Read cache metadata (gen, child_cursor, child_has_next) from Redis."""
        base = self._base_key(session_id)
        raw = await ctx.redis.get(f"{base}:meta")
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _write_cache(
        self,
        ctx: Any,
        session_id: str,
        data: List,
        gen: str,
        child_cursor: Dict,
        child_has_next: bool = False,
    ) -> None:
        """Write session data and metadata to Redis with TTL."""
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        base = self._base_key(session_id)
        ttl = self.cache.session_ttl

        pipe = ctx.redis.pipeline()
        pipe.set(base, orjson.dumps(data), ex=ttl)
        pipe.set(
            f"{base}:meta",
            orjson.dumps({"gen": gen, "child_cursor": child_cursor, "child_has_next": child_has_next}),
            ex=ttl,
        )
        await pipe.execute()

    async def _touch_ttl(self, ctx: Any, session_id: str) -> None:
        """Refresh TTL on data and meta keys (keeps cache alive while user scrolls)."""
        if not self.cache:
            return
        base = self._base_key(session_id)
        ttl = self.cache.session_ttl
        pipe = ctx.redis.pipeline()
        pipe.expire(base, ttl)
        pipe.expire(f"{base}:meta", ttl)
        await pipe.execute()

    # -- pagination ------------------------------------------------------------

    def _paginate(
        self,
        data: List,
        limit: int,
        offset: int,
        gen: str,
        child_has_next: bool = False,
    ) -> FeedResult:
        """Slice cached data at an absolute offset and build the next cursor.

        The cursor carries an absolute offset (not a page number), so pagination stays
        correct even if the client varies `limit` between requests.
        """
        end = offset + limit
        page_data = data[offset:end]
        has_next = end < len(data) or child_has_next

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
        methods_dict: dict,
        session_id: str,
        limit: int,
        cursor: dict,
        ctx: Any,
    ) -> FeedResult:
        """Cached path: warm hit -> paginate, stale/miss -> cold build."""
        my_cursor = cursor.get(self.node_id, {})
        cursor_gen = my_cursor.get("gen")
        cursor_offset = my_cursor.get("offset", 0)

        # Warm path: try to read existing cache
        if cursor_gen:
            meta = await self._read_meta(ctx, session_id)
            if meta and meta.get("gen") == cursor_gen:
                cached_data = await self._read_cache(ctx, session_id)
                if cached_data is not None:
                    # Serve from cache while this offset is still within the batch.
                    if cursor_offset < len(cached_data):
                        await self._touch_ttl(ctx, session_id)
                        child_has_next = meta.get("child_has_next", bool(meta.get("child_cursor")))
                        return self._paginate(
                            cached_data,
                            limit,
                            cursor_offset,
                            cursor_gen,
                            child_has_next=child_has_next,
                        )
                    # Cache exhausted: rebuild with continuation cursor
                    return await self._cold_build_locked(
                        methods_dict,
                        session_id,
                        limit,
                        ctx,
                        child_cursor=meta.get("child_cursor", {}),
                    )

        # Cold path: no gen or stale gen -> fresh build
        return await self._cold_build_locked(methods_dict, session_id, limit, ctx, child_cursor={})

    async def _cold_build_locked(
        self,
        methods_dict: dict,
        session_id: str,
        limit: int,
        ctx: Any,
        child_cursor: Dict,
    ) -> FeedResult:
        """Cold build guarded by a lock so concurrent first requests fetch the child
        ONCE: one caller builds and writes the cache, the rest wait and serve from it.
        A fresh/continuation build always serves the new batch from offset 0."""
        from smartfeed.execution.redis_lock import RedisLock

        lock_key = f"{self._base_key(session_id)}:coldlock"
        async with RedisLock(ctx.redis, lock_key, ttl=30) as acquired:
            if acquired:
                return await self._cold_build(methods_dict, session_id, limit, ctx, child_cursor)
            # Another coroutine is building -- wait for its cache, then serve page 1.
            for _ in range(50):
                await asyncio.sleep(0.1)
                meta = await self._read_meta(ctx, session_id)
                if meta:
                    cached = await self._read_cache(ctx, session_id)
                    if cached is not None:
                        child_has_next = meta.get("child_has_next", bool(meta.get("child_cursor")))
                        return self._paginate(
                            cached,
                            limit,
                            0,
                            meta.get("gen", ""),
                            child_has_next=child_has_next,
                        )
            # Fallback: builder never wrote -> build ourselves.
            return await self._cold_build(methods_dict, session_id, limit, ctx, child_cursor)

    # -- shared base cache helpers ----------------------------------------------

    @staticmethod
    def _cursor_segment(child_cursor: Dict) -> str:
        """Stable short tag for a continuation position, so each page-window of the
        shared base is its own segment (page 1 = "0")."""
        if not child_cursor:
            return "0"
        raw = json.dumps(child_cursor, sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:8]

    def _base_shared_key(self, session_id: str, child_cursor: Dict) -> str:
        """Key for a shared-base segment (deduped, NOT reranked), keyed by cache_key
        and the continuation position so continuations don't collide with page 1."""
        seg = self._cursor_segment(child_cursor)
        return f"sf:{session_id}:{self.cache_key}:{self.data.config_hash()}:{seg}"

    async def _read_shared_base(self, ctx: Any, session_id: str, child_cursor: Dict) -> Optional[List]:
        """Read a shared-base segment from Redis. Returns None on miss."""
        key = self._base_shared_key(session_id, child_cursor)
        raw = await ctx.redis.get(key)
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _write_shared_base(
        self,
        ctx: Any,
        session_id: str,
        child_cursor: Dict,
        data: List,
        child_cursor_out: Dict,
        child_has_next: bool = False,
    ) -> None:
        """Write a shared-base segment and its meta to Redis with TTL."""
        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        key = self._base_shared_key(session_id, child_cursor)
        ttl = self.cache.session_ttl
        pipe = ctx.redis.pipeline()
        pipe.set(key, orjson.dumps(data), ex=ttl)
        pipe.set(
            f"{key}:meta",
            orjson.dumps({"child_cursor": child_cursor_out, "child_has_next": child_has_next}),
            ex=ttl,
        )
        await pipe.execute()

    async def _read_shared_base_meta(self, ctx: Any, session_id: str, child_cursor: Dict) -> Optional[Dict]:
        """Read a shared-base segment's metadata (child_cursor, child_has_next)."""
        key = self._base_shared_key(session_id, child_cursor)
        raw = await ctx.redis.get(f"{key}:meta")
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _fetch_and_dedup(
        self,
        ctx: Any,
        target: int,
        child_cursor: Dict,
        seen: Optional[set] = None,
    ) -> tuple:
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
        data: List = []
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
        methods_dict: dict,
        session_id: str,
        limit: int,
        ctx: Any,
        child_cursor: Dict,
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
            seen: Optional[set] = None
            if self.dedup:
                if not child_cursor:
                    await self._reset_seen_set(ctx, session_id)
                    seen = set()
                else:
                    seen = await self._load_seen_set(ctx, session_id)
            data, child_cursor_out, child_has_next = await self._fetch_and_dedup(
                ctx, session_size, child_cursor, seen=seen
            )
            if self.dedup:
                assert seen is not None
                await self._save_seen_set(ctx, session_id, seen)
            self._stamp_pre_rerank(data)
            data = await self._apply_rerank(data, methods_dict, session_id)
            if self.rerank:
                self._stamp_post_rerank(data)

        gen = secrets.token_hex(4)
        await self._write_cache(ctx, session_id, data, gen, child_cursor_out, child_has_next)
        return self._paginate(data, limit, 0, gen, child_has_next=child_has_next)

    async def _build_shared_base(
        self,
        methods_dict: dict,
        session_id: str,
        ctx: Any,
        child_cursor: Dict,
    ) -> List:
        """Fetch and dedup the shared base data, using a distributed lock to ensure
        only one caller fetches from child on a cold build.  Others wait and read."""
        from smartfeed.execution.redis_lock import RedisLock

        assert self.cache is not None, "cached path; execute() routes cache=None to passthrough"
        session_size = self.cache.session_size
        lock_key = f"{self._base_shared_key(session_id, child_cursor)}:lock"

        # Fast path: this segment already cached
        existing = await self._read_shared_base(ctx, session_id, child_cursor)
        if existing is not None:
            return existing

        # Try to acquire the lock
        async with RedisLock(ctx.redis, lock_key, ttl=30) as acquired:
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
            else:
                # Another coroutine holds the lock -- poll until the segment appears
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    existing = await self._read_shared_base(ctx, session_id, child_cursor)
                    if existing is not None:
                        return existing
                # Fallback: fetch ourselves if lock holder never wrote
                existing = await self._read_shared_base(ctx, session_id, child_cursor)
                if existing is not None:
                    return existing
                data, _, _ = await self._fetch_and_dedup(ctx, session_size, child_cursor)
                return data
