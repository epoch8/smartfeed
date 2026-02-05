from __future__ import annotations

import asyncio
from collections import defaultdict
from random import shuffle
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Literal, Optional, Union, cast

import redis
from redis.asyncio import Redis as AsyncRedis

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, _pydantic_deep_copy

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerAppend(BaseFeedConfigModel):
    """Append merger."""

    merger_id: str
    type: Literal["merger_append"]
    items: List[FeedTypes]
    shuffle: bool = False

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        dedup_active = bool(params.pop("_sf_dedup_active", False))

        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        if dedup_active:
            indexed_items = list(enumerate(self.items))
            fetched: Dict[int, FeedResult] = {}

            groups: Dict[int, List[tuple[int, "FeedTypes"]]] = defaultdict(list)
            for idx, item in indexed_items:
                prio = int(getattr(item, "dedup_priority", 0))
                groups[prio].append((idx, item))

            for prio in sorted(groups.keys(), reverse=True):
                group = groups[prio]
                coros: List[Awaitable[FeedResult]] = []
                order: List[int] = []
                for idx, item in group:
                    order.append(idx)
                    coros.append(
                        item.get_data(
                            methods_dict=methods_dict,
                            user_id=user_id,
                            limit=limit,
                            next_page=_pydantic_deep_copy(next_page),
                            redis_client=redis_client,
                            _sf_dedup_active=True,
                            **params,
                        )
                    )
                group_results = await asyncio.gather(*coros)
                for idx, r in zip(order, group_results):
                    fetched[idx] = cast(FeedResult, r)

            for idx, _item in indexed_items:
                item_result = fetched[idx]
                result.data.extend(item_result.data)
                result.next_page.data.update(item_result.next_page.data)
                if item_result.has_next_page:
                    result.has_next_page = True

            if len(result.data) > limit:
                result.data = result.data[:limit]
        else:
            result_limit = limit
            for item in self.items:
                item_result = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=result_limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    **params,
                )

                result.data.extend(item_result.data)
                result_limit -= len(item_result.data)

                if not result.has_next_page and item_result.has_next_page:
                    result.has_next_page = True

                result.next_page.data.update(item_result.next_page.data)

                if result_limit <= 0:
                    break

        if self.shuffle:
            shuffle(result.data)

        return result
