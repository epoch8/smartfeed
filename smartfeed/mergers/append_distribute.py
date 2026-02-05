from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis
from typing_extensions import no_type_check

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerAppendDistribute(BaseFeedConfigModel):
    """Merger that uniformly distributes items by a key."""

    merger_id: str
    type: Literal["merger_distribute"]
    items: List["FeedTypes"]
    distribution_key: str
    sorting_key: Optional[str] = None
    sorting_desc: bool = False

    @no_type_check
    async def _uniform_distribute(self, data: list) -> list:
        if self.sorting_key:
            data = sorted(data, key=lambda x: x[self.sorting_key], reverse=self.sorting_desc)

        grouped_entries = defaultdict(deque)
        for entry in data:
            grouped_entries[entry[self.distribution_key]].append(entry)
        result = []
        prev_profile_id = None
        while any(grouped_entries.values()):
            for profile_id in list(grouped_entries.keys()):
                if grouped_entries[profile_id]:
                    if profile_id != prev_profile_id or len(grouped_entries) == 1:
                        result.append(grouped_entries[profile_id].popleft())
                        prev_profile_id = profile_id
                    if not grouped_entries[profile_id]:
                        del grouped_entries[profile_id]
                else:
                    del grouped_entries[profile_id]

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
        dedup_active = bool(params.pop("_sf_dedup_active", False))

        result = FeedResult(data=[], next_page=FeedResultNextPage(data={}), has_next_page=False)

        if dedup_active:
            indexed_items = list(enumerate(self.items))
            fetch_order = sorted(indexed_items, key=lambda p: (getattr(p[1], "dedup_priority", 0), -p[0]), reverse=True)
            fetched: Dict[int, FeedResult] = {}

            for idx, item in fetch_order:
                fetched[idx] = await item.get_data(
                    methods_dict=methods_dict,
                    user_id=user_id,
                    limit=limit,
                    next_page=next_page,
                    redis_client=redis_client,
                    _sf_dedup_active=True,
                    **params,
                )

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

        result.data = await self._uniform_distribute(result.data)
        return result
