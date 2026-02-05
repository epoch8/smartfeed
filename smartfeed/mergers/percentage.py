from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union, cast

import redis
from pydantic import BaseModel
from redis.asyncio import Redis as AsyncRedis

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
    async def _merge_items_data(items_data: List[List]) -> List:
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
        **params: Any,
    ) -> FeedResult:
        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        items_data: List[List[Any]] = [[] for _ in self.items]
        results: List[Optional[FeedResult]] = [None for _ in self.items]

        indexed_items = list(enumerate(self.items))
        fetch_order = indexed_items
        if dedup_active:
            fetch_order = sorted(
                indexed_items,
                key=lambda p: (getattr(p[1].data, "dedup_priority", 0), -p[0]),
                reverse=True,
            )

        for idx, item in fetch_order:
            item_result = cast(
                FeedResult,
                await item.data.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=limit * item.percentage // 100,
                    next_page=next_page,
                    redis_client=redis_client,
                    _sf_dedup_active=dedup_active,
                    **params,
                ),
            )

            results[idx] = item_result

        for idx, result_item in enumerate(results):
            assert result_item is not None
            items_data[idx] = result_item.data

            if not result.has_next_page and result_item.has_next_page:
                result.has_next_page = True
            result.next_page.data.update(result_item.next_page.data)

        result.data = await self._merge_items_data(items_data=items_data)

        if self.shuffle:
            shuffle(result.data)

        return result
