from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union, cast

import redis
from pydantic import BaseModel
from redis.asyncio import Redis as AsyncRedis

from ..execution.context import ExecutionContext
from ..execution.executor import SlotSpec, SlotsPlan
from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerPercentageItem(BaseModel):
    """One percentage slot."""

    percentage: int
    data: FeedTypes


class MergerPercentage(BaseFeedConfigModel):
    """Percentage-based mixing merger."""

    merger_id: str
    type: Literal["merger_percentage"]
    items: List[MergerPercentageItem]
    shuffle: bool = False

    @staticmethod
    def _merge_items_data(items_data: List[List]) -> List:
        result: List = []
        cursor: List[Dict] = []

        min_length = min(len(item_data) for item_data in items_data) or 1
        for item_data in items_data:
            cursor.append(
                {
                    "items": item_data,
                    "current": 0,
                    "size": round(len(item_data) / min_length),
                }
            )

        full_length = sum(len(item_data) for item_data in items_data)
        while len(result) < full_length:
            for item_cursor in cursor:
                items = item_cursor["items"]
                start = item_cursor["current"]
                end = start + item_cursor["size"] if start + item_cursor["size"] < len(items) else len(items)
                result.extend(items[start:end])
                item_cursor["current"] = end

        return result

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        ctx: Optional[ExecutionContext] = None,
        **params: Any,
    ) -> FeedResult:
        if ctx is None:
            ctx = ExecutionContext(methods_dict=methods_dict, user_id=user_id, redis_client=redis_client)
        else:
            ctx.ensure_redis_client(redis_client)

        executor = ctx.ensure_executor()
        return await executor.run(self, ctx, limit, next_page, **params)

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> SlotsPlan:
        owners: List[BaseFeedConfigModel] = [cast(BaseFeedConfigModel, item.data) for item in self.items]

        slots: List[SlotSpec] = []
        for item, owner in zip(self.items, owners):
            child_limit = limit * int(item.percentage) // 100
            slots.append(SlotSpec(owner=owner, max_count=max(0, child_limit)))

        def _assemble(
            output: List[Any],
            merged_next_page: FeedResultNextPage,
            owner_results: Dict[int, FeedResult],
        ) -> FeedResult:
            items_data: List[List[Any]] = []
            has_next_page = False

            for owner in owners:
                child_res = owner_results.get(id(owner))
                if child_res is None:
                    items_data.append([])
                    continue
                items_data.append(list(child_res.data))
                has_next_page = has_next_page or bool(child_res.has_next_page)

            data = self._merge_items_data(items_data=items_data)
            if self.shuffle:
                shuffle(data)

            return FeedResult(data=data, next_page=merged_next_page, has_next_page=has_next_page)

        return SlotsPlan(
            ctx=ctx,
            limit=limit,
            next_page=next_page,
            params=dict(params),
            slots=slots,
            assemble=_assemble,
        )
