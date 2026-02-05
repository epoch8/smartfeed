import asyncio
import inspect
from abc import ABC, abstractmethod
from dataclasses import dataclass
from random import shuffle
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Union, cast

import redis
from pydantic import BaseModel
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio import RedisCluster as AsyncRedisCluster


def _is_async_redis_client(client: Any) -> bool:
    return isinstance(client, (AsyncRedis, AsyncRedisCluster))


async def _redis_call(client: Any, method_name: str, *args: Any, **kwargs: Any) -> Any:
    """Call a Redis method without blocking the event loop.

    - For `redis.asyncio` clients, calls are awaited directly.
    - For sync `redis.Redis`, calls are offloaded via `asyncio.to_thread()`.
    """

    method = getattr(client, method_name)
    if _is_async_redis_client(client):
        return await method(*args, **kwargs)
    return await asyncio.to_thread(method, *args, **kwargs)


def _pydantic_deep_copy(model: Any) -> Any:
    """Deep copy helper compatible with Pydantic v1 and v2."""

    if hasattr(model, "model_copy"):
        return model.model_copy(deep=True)
    return model.copy(deep=True)


class FeedResultNextPageInside(BaseModel):
    """Cursor model for one feed node."""

    page: int = 1
    after: Any = None


class FeedResultNextPage(BaseModel):
    """Cursor model for a whole feed traversal."""

    data: Dict[str, FeedResultNextPageInside]


class FeedResult(BaseModel):
    """Normalized output of any feed node `get_data()`."""

    data: List
    next_page: FeedResultNextPage
    has_next_page: bool


class FeedResultClient(BaseModel):
    """Result returned by client subfeed methods."""

    data: List
    next_page: FeedResultNextPageInside
    has_next_page: bool


class BaseFeedConfigModel(ABC, BaseModel):
    """Base class for merger/subfeed config models."""

    # Higher value means the item should "win" deduplication when duplicates exist.
    # This is primarily used by MergerDeduplication and by mergers when a dedup wrapper is active.
    dedup_priority: int = 0

    @abstractmethod
    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        """Fetch data according to this node config."""


@dataclass
class _SubFeedMethodSpec:
    method: Callable
    args: List[str]


class SubFeed(BaseFeedConfigModel):
    """Leaf node pointing at a client method."""

    subfeed_id: str
    type: Literal["subfeed"]
    method_name: str
    subfeed_params: Dict[str, Any] = {}
    raise_error: Optional[bool] = True
    shuffle: bool = False

    def _get_method_spec(self, methods_dict: Dict[str, Callable]) -> _SubFeedMethodSpec:
        method = methods_dict[self.method_name]
        method_spec = getattr(method, "_smartfeed_original", method)
        method_args = inspect.getfullargspec(method_spec).args
        return _SubFeedMethodSpec(method=method, args=list(method_args))

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        subfeed_next_page = FeedResultNextPageInside(
            page=next_page.data[self.subfeed_id].page if self.subfeed_id in next_page.data else 1,
            after=next_page.data[self.subfeed_id].after if self.subfeed_id in next_page.data else None,
        )

        method_spec = self._get_method_spec(methods_dict)

        method_params: Dict[str, Any] = {}
        for arg in method_spec.args:
            if arg in params:
                method_params[arg] = params[arg]

        try:
            method_result = await method_spec.method(
                user_id=user_id,
                limit=limit,
                next_page=subfeed_next_page,
                **method_params,
                **self.subfeed_params,
            )
        except Exception:
            if self.raise_error:
                raise

            method_result = FeedResultClient(
                data=[],
                next_page=subfeed_next_page,
                has_next_page=False,
            )

        if not isinstance(method_result, FeedResultClient):
            raise TypeError('SubFeed function must return "FeedResultClient" instance.')

        if self.shuffle:
            shuffle(method_result.data)

        return FeedResult(
            data=method_result.data,
            next_page=FeedResultNextPage(
                data={self.subfeed_id: cast(FeedResultNextPageInside, method_result.next_page)}
            ),
            has_next_page=bool(method_result.has_next_page),
        )
