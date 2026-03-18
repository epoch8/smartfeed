from __future__ import annotations

import logging
from random import shuffle
from typing import TYPE_CHECKING, Any, List, Literal, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis

from .. import jsonlib as json
from ..execution.context import ExecutionContext
from ..execution.executor import CallablePlan
from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, FeedResultNextPageInside, _redis_call
from ..policies.dedup import entity_key

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerViewSession(BaseFeedConfigModel):
    """Merger with view-session caching."""

    merger_id: str
    type: Literal["merger_view_session"]
    session_size: int
    session_live_time: int
    data: "FeedTypes"
    deduplicate: bool = False
    dedup_key: Optional[str] = None
    missing_key_policy: Literal["error", "keep", "drop"] = "error"
    shuffle: bool = False

    def _get_dedup_key_or_attr(self, item: Any) -> str:
        key = entity_key(item, self.dedup_key, self.missing_key_policy)
        assert key is not None, "Deduplication key is missing and item was dropped by missing_key_policy='drop'"
        return key

    def _dedup_data(self, data: List[Any]) -> List[Any]:
        deduplicated: List[Any] = []
        seen: set[str] = set()
        for item in data:
            key = entity_key(item, self.dedup_key, self.missing_key_policy)
            if key is None:
                continue
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)
        return deduplicated

    async def _set_cache(
        self,
        redis_client: Union[redis.Redis, AsyncRedis],
        cache_key: str,
        ctx: ExecutionContext,
        child_next_page: Optional[FeedResultNextPage] = None,
        **params: Any,
    ) -> tuple[List[Any], bool, Optional[FeedResultNextPage]]:
        if ctx.executor is None:
            raise ValueError("Executor must be initialized for MergerViewSession")

        inner_dedup = ctx.dedup.create_isolated() if ctx.dedup is not None else None
        inner_ctx = ExecutionContext(
            methods_dict=ctx.methods_dict,
            user_id=ctx.user_id,
            redis_client=ctx.redis_client,
            executor=ctx.executor,
            dedup=inner_dedup,
            refill_settings=ctx.refill_settings if inner_dedup is not None else None,
        )
        start_cursor = child_next_page if child_next_page is not None else FeedResultNextPage(data={})
        result = await ctx.executor.run(self.data, inner_ctx, self.session_size, start_cursor, **params)

        data = result.data
        if self.deduplicate:
            data = self._dedup_data(data)
        await _redis_call(redis_client, "set", cache_key, json.dumps(data), ex=self.session_live_time)

        meta = {
            "child_has_next": result.has_next_page,
            "child_cursor": result.next_page.model_dump(),
        }
        await _redis_call(redis_client, "set", f"{cache_key}:meta", json.dumps(meta), ex=self.session_live_time)

        child_cursor = result.next_page if result.has_next_page else None
        return data, result.has_next_page, child_cursor

    async def _get_cache(
        self,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Union[redis.Redis, AsyncRedis],
        ctx: ExecutionContext,
        **params: Any,
    ) -> FeedResult:
        cache_key = (
            f"{self.merger_id}_{ctx.user_id}_{session_cache_key}"
            if (session_cache_key := params.get("custom_view_session_key"))
            else f"{self.merger_id}_{ctx.user_id}"
        )

        logging.info("MergerViewSession cache request for %s", cache_key)

        child_has_next = False
        child_cursor: Optional[FeedResultNextPage] = None

        cache_exists = bool(await _redis_call(redis_client, "exists", cache_key))
        if not cache_exists or self.merger_id not in next_page.data:
            logging.info("Cache miss or new session - generating fresh data for %s", cache_key)
            session_data, child_has_next, child_cursor = await self._set_cache(
                redis_client=redis_client,
                cache_key=cache_key,
                ctx=ctx,
                **params,
            )
        else:
            logging.info("Cache exists - attempting read from Redis for %s", cache_key)
            cached_data = await _redis_call(redis_client, "get", cache_key)
            if cached_data is None:
                logging.info(
                    "Redis returned None for %s - falling back to fresh data (cluster replication issue)", cache_key
                )
                session_data, child_has_next, child_cursor = await self._set_cache(
                    redis_client=redis_client,
                    cache_key=cache_key,
                    ctx=ctx,
                    **params,
                )
            else:
                logging.info("Successfully read cached data for %s", cache_key)
                session_data = json.loads(cached_data)

                meta_raw = await _redis_call(redis_client, "get", f"{cache_key}:meta")
                if meta_raw:
                    meta = json.loads(meta_raw)
                    child_has_next = meta.get("child_has_next", False)
                    child_cursor_data = meta.get("child_cursor")
                    if child_cursor_data:
                        child_cursor = FeedResultNextPage.model_validate(child_cursor_data)

        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1

        # Session exhausted but child has more data -- rebuild from continuation cursor
        if (page - 1) * limit >= len(session_data) and child_has_next and child_cursor is not None:
            logging.info("Session exhausted at page %d for %s - rebuilding with child cursor", page, cache_key)
            session_data, child_has_next, child_cursor = await self._set_cache(
                redis_client=redis_client,
                cache_key=cache_key,
                ctx=ctx,
                child_next_page=child_cursor,
                **params,
            )
            page = 1

        return FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(data={self.merger_id: FeedResultNextPageInside(page=page + 1, after=None)}),
            has_next_page=bool(len(session_data) > limit * page or child_has_next),
        )

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> CallablePlan:
        async def _run(executor: Any) -> FeedResult:
            if ctx.redis_client is None:
                raise ValueError("Redis client must be provided if using Merger View Session")

            if ctx.executor is None:
                ctx.executor = executor

            result = await self._get_cache(
                limit=limit,
                next_page=next_page,
                redis_client=ctx.redis_client,
                ctx=ctx,
                **params,
            )

            if self.shuffle:
                shuffle(result.data)
            return result

        return CallablePlan(fn=_run)
