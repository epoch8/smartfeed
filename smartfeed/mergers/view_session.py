from __future__ import annotations

import logging
from random import shuffle
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import RedisCluster as AsyncRedisCluster

from .. import jsonlib as json
from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, FeedResultNextPageInside, _redis_call

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
    dedup_key: str = None  # type: ignore
    shuffle: bool = False

    def _get_dedup_key_or_attr(self, item: Any) -> str:
        if not self.dedup_key:
            return item

        try:
            dedup_value = item.get(self.dedup_key)
        except AttributeError:
            dedup_value = getattr(item, self.dedup_key, None)

        assert dedup_value is not None, f"Deduplication failed: entity {item} has no key or attr {self.dedup_key}"
        return dedup_value

    def _dedup_data(self, data: List[Any]) -> List[Any]:
        deduplicated_data = {self._get_dedup_key_or_attr(item): item for item in data}
        return list(deduplicated_data.values())

    async def _set_cache(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        redis_client: Union[redis.Redis, AsyncRedis],
        cache_key: str,
        **params: Any,
    ) -> List[Any]:
        result = await self.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=self.session_size,
            next_page=FeedResultNextPage(data={}),
            **params,
        )

        data = result.data
        if self.deduplicate:
            data = self._dedup_data(data)
        await _redis_call(redis_client, "set", cache_key, json.dumps(data), ex=self.session_live_time)
        return data

    async def _set_cache_async(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        redis_client: AsyncRedis,
        cache_key: str,
        **params: Any,
    ) -> List[Any]:
        result = await self.data.get_data(
            methods_dict=methods_dict,
            user_id=user_id,
            limit=self.session_size,
            next_page=FeedResultNextPage(data={}),
            **params,
        )

        data = result.data
        if self.deduplicate:
            data = self._dedup_data(data)
        await redis_client.set(cache_key, json.dumps(data))
        await redis_client.expire(cache_key, self.session_live_time)
        return data

    async def _get_cache(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Union[redis.Redis, AsyncRedis],
        **params: Any,
    ) -> FeedResult:
        if session_cache_key := params.get("custom_view_session_key", None):
            cache_key = f"{self.merger_id}_{user_id}_{session_cache_key}"
        else:
            cache_key = f"{self.merger_id}_{user_id}"

        logging.info("MergerViewSession cache request for %s", cache_key)
        cache_exists = bool(await _redis_call(redis_client, "exists", cache_key))
        if not cache_exists or self.merger_id not in next_page.data:
            logging.info("Cache miss or new session - generating fresh data for %s", cache_key)
            session_data = await self._set_cache(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )
        else:
            logging.info("Cache exists - attempting read from Redis for %s", cache_key)
            cached_data = await _redis_call(redis_client, "get", cache_key)
            if cached_data is None:
                logging.info(
                    "Redis returned None for %s - falling back to fresh data (cluster replication issue)", cache_key
                )
                session_data = await self._set_cache(
                    methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
                )
            else:
                logging.info("Successfully read cached data for %s", cache_key)
                session_data = json.loads(cached_data)

        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        return FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(data={self.merger_id: FeedResultNextPageInside(page=page + 1, after=None)}),
            has_next_page=bool(len(session_data) > limit * page),
        )

    async def _get_cache_async(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: AsyncRedis,
        **params: Any,
    ) -> FeedResult:
        if session_cache_key := params.get("custom_view_session_key", None):
            cache_key = f"{self.merger_id}_{user_id}_{session_cache_key}"
        else:
            cache_key = f"{self.merger_id}_{user_id}"

        if not await redis_client.exists(cache_key) or self.merger_id not in next_page.data:
            session_data = await self._set_cache_async(
                methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
            )
        else:
            cached_data = await redis_client.get(cache_key)
            if cached_data is None:
                logging.info(
                    "Redis returned None for %s - falling back to fresh data (cluster replication issue)", cache_key
                )
                session_data = await self._set_cache_async(
                    methods_dict=methods_dict, user_id=user_id, redis_client=redis_client, cache_key=cache_key, **params
                )
            else:
                logging.info("Successfully read cached data for %s", cache_key)
                session_data = json.loads(cached_data)

        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        return FeedResult(
            data=session_data[(page - 1) * limit :][:limit],
            next_page=FeedResultNextPage(data={self.merger_id: FeedResultNextPageInside(page=page + 1, after=None)}),
            has_next_page=bool(len(session_data) > limit * page),
        )

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        if not redis_client:
            raise ValueError("Redis client must be provided if using Merger View Session")

        if isinstance(redis_client, (AsyncRedis, AsyncRedisCluster)):
            result = await self._get_cache_async(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
            )
        else:
            result = await self._get_cache(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                **params,
            )

        if self.shuffle:
            shuffle(result.data)

        return result
