from __future__ import annotations

import asyncio
import secrets
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, model_validator

import orjson
from smartfeed.execution import executor as _executor
from .base import BaseNode, FeedResult, coerce_feed_node


class WrapperCache(BaseModel):
    session_size: int = 300
    session_ttl: int = 300


class WrapperRerank(BaseModel):
    method_name: str
    raise_error: bool = True  # True = crash if rerank fails, False = keep original order


class WrapperDedup(BaseModel):
    dedup_key: str
    missing_key_policy: Literal["error", "keep", "drop"] = "error"
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

        return await self._execute_with_cache(
            methods_dict, session_id, limit, cursor, ctx
        )

    # -- dedup -----------------------------------------------------------------

    def _resolve_dedup_priorities(self) -> Dict[str, int]:
        """Build {subfeed_id: effective_priority} by walking the config tree."""
        result: Dict[str, int] = {}
        self._collect_priorities(self.data, override_priority=0, out=result)
        return result

    @staticmethod
    def _collect_priorities(
        node: BaseNode, override_priority: int, out: Dict[str, int]
    ) -> None:
        """Recursive walk: propagate override_priority down, write SubFeed priorities to out."""
        from .subfeed import SubFeed

        # Determine the effective priority for this subtree.
        # A node with dedup_priority != 0 overrides all children.
        effective = node.dedup_priority if node.dedup_priority != 0 else override_priority

        if isinstance(node, SubFeed):
            out[node.subfeed_id] = effective
            return

        # Walk children: look in known child-bearing attributes
        for attr in ("items", "data", "positional", "default",
                      "item_from", "item_to"):
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

            # Cross-page: already seen on previous pages
            if key_val in seen:
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

    async def _apply_rerank(
        self, data: list, methods_dict: dict, session_id: str
    ) -> list:
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
                f"Rerank '{self.rerank.method_name}' must return exactly "
                f"{original_len} items, got {len(result)}"
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
        """No-cache path: fetch -> dedup (with overfetch/refill) -> rerank -> return."""
        if self.dedup:
            # Load persisted seen-set from Redis (cross-page dedup)
            seen_keys = await self._load_seen_set(ctx, session_id)

            overfetch = self.dedup.overfetch_factor
            max_loops = self.dedup.max_refill_loops
            fetch_size = limit * overfetch
            current_cursor = dict(cursor)

            result = await _executor.run(self.data, ctx, fetch_size, current_cursor)
            data = self._dedup(result.data, seen_keys)
            current_cursor = result.next_page
            has_next = result.has_next_page

            loop = 0
            while len(data) < limit and has_next and loop < max_loops:
                deficit = limit - len(data)
                result = await _executor.run(self.data, ctx, deficit * overfetch, current_cursor)
                data.extend(self._dedup(result.data, seen_keys))
                current_cursor = result.next_page
                has_next = result.has_next_page
                loop += 1

            data = data[:limit]

            # Persist seen-set to Redis for next page
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
        ttl = self.dedup.state_ttl
        await ctx.redis.expire(key, ttl)

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
        self, data: List, limit: int, page: int, gen: str,
        child_has_next: bool = False,
    ) -> FeedResult:
        """Slice cached data for the requested page and build cursor."""
        start = (page - 1) * limit
        end = start + limit
        page_data = data[start:end]
        has_next = end < len(data) or child_has_next

        next_cursor = {
            self.node_id: {
                "page": page + 1,
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
        cursor_page = my_cursor.get("page", 1)

        # Warm path: try to read existing cache
        if cursor_gen:
            meta = await self._read_meta(ctx, session_id)
            if meta and meta.get("gen") == cursor_gen:
                cached_data = await self._read_cache(ctx, session_id)
                if cached_data is not None:
                    # Check if we have enough data for this page
                    start = (cursor_page - 1) * limit
                    if start < len(cached_data):
                        await self._touch_ttl(ctx, session_id)
                        child_has_next = meta.get("child_has_next", bool(meta.get("child_cursor")))
                        return self._paginate(
                            cached_data, limit, cursor_page, cursor_gen,
                            child_has_next=child_has_next,
                        )
                    # Cache exhausted: rebuild with continuation cursor
                    return await self._cold_build(
                        methods_dict,
                        session_id,
                        limit,
                        ctx,
                        child_cursor=meta.get("child_cursor", {}),
                    )

        # Cold path: no gen or stale gen -> fresh build
        return await self._cold_build(
            methods_dict, session_id, limit, ctx, child_cursor={}
        )

    # -- shared base cache helpers ----------------------------------------------

    def _base_shared_key(self, session_id: str) -> str:
        """Key for the shared base cache (deduped, NOT reranked), keyed by cache_key."""
        return f"sf:{session_id}:{self.cache_key}:{self.data.config_hash()}"

    async def _read_shared_base(self, ctx: Any, session_id: str) -> Optional[List]:
        """Read shared base data from Redis. Returns None on miss."""
        key = self._base_shared_key(session_id)
        raw = await ctx.redis.get(key)
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _write_shared_base(
        self, ctx: Any, session_id: str, data: List, child_cursor: Dict,
        child_has_next: bool = False,
    ) -> None:
        """Write shared base data and meta to Redis with TTL."""
        key = self._base_shared_key(session_id)
        ttl = self.cache.session_ttl
        pipe = ctx.redis.pipeline()
        pipe.set(key, orjson.dumps(data), ex=ttl)
        pipe.set(
            f"{key}:meta",
            orjson.dumps({"child_cursor": child_cursor, "child_has_next": child_has_next}),
            ex=ttl,
        )
        await pipe.execute()

    async def _read_shared_base_meta(self, ctx: Any, session_id: str) -> Optional[Dict]:
        """Read shared base metadata (child_cursor, child_has_next) from Redis."""
        key = self._base_shared_key(session_id)
        raw = await ctx.redis.get(f"{key}:meta")
        if raw is None:
            return None
        return orjson.loads(raw)

    async def _fetch_and_dedup(
        self,
        ctx: Any,
        target: int,
        child_cursor: Dict,
    ) -> tuple:
        """Fetch target items from child with overfetch + refill loop for dedup.

        Returns (data, child_cursor, child_has_next).
        """
        overfetch = self.dedup.overfetch_factor if self.dedup else 1
        max_loops = self.dedup.max_refill_loops if self.dedup else 0
        fetch_size = target * overfetch
        cursor = dict(child_cursor)

        # First fetch
        result = await _executor.run(self.data, ctx, fetch_size, cursor)
        data = self._dedup(result.data) if self.dedup else result.data
        cursor = result.next_page
        has_next = result.has_next_page

        # Refill loop: keep fetching if dedup ate too many items.
        # Accumulate seen keys so refill batches are deduped against all prior items.
        loop = 0
        while len(data) < target and has_next and loop < max_loops:
            deficit = target - len(data)
            refill_size = deficit * overfetch
            result = await _executor.run(self.data, ctx, refill_size, cursor)
            # Dedup refill against ALL accumulated data
            combined = data + result.data
            combined = self._dedup(combined) if self.dedup else combined
            data = combined
            cursor = result.next_page
            has_next = result.has_next_page
            loop += 1

        return data[:target], cursor, has_next

    async def _cold_build(
        self,
        methods_dict: dict,
        session_id: str,
        limit: int,
        ctx: Any,
        child_cursor: Dict,
    ) -> FeedResult:
        """Build full session: fetch -> dedup -> rerank -> write cache -> paginate page 1."""
        session_size = self.cache.session_size

        if self.cache_key is not None:
            # Shared cache path: base data (fetch + dedup) is shared across wrappers
            # with the same cache_key; rerank is applied per-wrapper after.
            base_data = await self._build_shared_base(
                methods_dict, session_id, ctx, child_cursor
            )
            # Read child_cursor from shared base meta for continuation
            shared_meta = await self._read_shared_base_meta(ctx, session_id)
            child_cursor_out = shared_meta.get("child_cursor", {}) if shared_meta else {}
            child_has_next = shared_meta.get("child_has_next", False) if shared_meta else False
            # Per-wrapper: stamp pre-rerank, apply rerank, stamp post-rerank
            data = list(base_data)
            self._stamp_pre_rerank(data)
            data = await self._apply_rerank(data, methods_dict, session_id)
            if self.rerank:
                self._stamp_post_rerank(data)
        else:
            # Standard path: fetch -> dedup (with overfetch/refill) -> rerank -> stamp -> cache
            data, child_cursor_out, child_has_next = await self._fetch_and_dedup(
                ctx, session_size, child_cursor
            )
            self._stamp_pre_rerank(data)
            data = await self._apply_rerank(data, methods_dict, session_id)
            if self.rerank:
                self._stamp_post_rerank(data)

        gen = secrets.token_hex(4)
        await self._write_cache(ctx, session_id, data, gen, child_cursor_out, child_has_next)
        return self._paginate(data, limit, 1, gen, child_has_next=child_has_next)

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

        session_size = self.cache.session_size
        lock_key = f"{self._base_shared_key(session_id)}:lock"

        # Fast path: base already cached
        existing = await self._read_shared_base(ctx, session_id)
        if existing is not None:
            return existing

        # Try to acquire the lock
        async with RedisLock(ctx.redis, lock_key, ttl=30) as acquired:
            if acquired:
                # Double-check after acquiring lock
                existing = await self._read_shared_base(ctx, session_id)
                if existing is not None:
                    return existing

                base_data, child_cursor_out, child_has_next = await self._fetch_and_dedup(
                    ctx, session_size, child_cursor
                )
                await self._write_shared_base(ctx, session_id, base_data, child_cursor_out, child_has_next)
                return base_data
            else:
                # Another coroutine holds the lock -- poll until base cache appears
                for _ in range(50):
                    await asyncio.sleep(0.1)
                    existing = await self._read_shared_base(ctx, session_id)
                    if existing is not None:
                        return existing
                # Fallback: fetch ourselves if lock holder never wrote
                existing = await self._read_shared_base(ctx, session_id)
                if existing is not None:
                    return existing
                data, _, _ = await self._fetch_and_dedup(ctx, session_size, child_cursor)
                return data
