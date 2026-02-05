from random import shuffle
from typing import Any, Callable, Dict, Literal, Optional, Union

import redis
from pydantic import model_validator
from redis.asyncio import Redis as AsyncRedis

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, FeedResultNextPageInside
from .percentage import MergerPercentageItem


class MergerPercentageGradient(BaseFeedConfigModel):
    """Percentage-gradient merger."""

    merger_id: str
    type: Literal["merger_percentage_gradient"]
    item_from: MergerPercentageItem
    item_to: MergerPercentageItem
    step: int
    size_to_step: int
    shuffle: bool = False

    @model_validator(mode="after")
    def validate_merger_percentage_gradient(self) -> "MergerPercentageGradient":
        if self.step < 1 or self.step > 100:
            raise ValueError('"step" must be in range from 1 to 100')
        if self.size_to_step < 1:
            raise ValueError('"size_to_step" must be bigger than 1')
        return self

    async def _calculate_limits_and_percents(self, page: int, limit: int) -> Dict:
        result: Dict = {
            "limit_from": 0,
            "limit_to": 0,
            "percentages": [],
        }

        percentage_from = self.item_from.percentage
        percentage_to = self.item_to.percentage
        start_position = limit * (page - 1)
        first_iter = True

        for i in range(self.size_to_step, limit * page + self.size_to_step, self.size_to_step):
            if not first_iter and percentage_to < 100:
                percentage_from -= self.step
                percentage_to += self.step

                if percentage_to > 100 or percentage_from < 0:
                    percentage_from = 0
                    percentage_to = 100

            if i > start_position:
                iter_limit = (limit * page - start_position) if i > limit * page else (i - start_position)
                start_position = i

                if result["percentages"] and result["percentages"][-1]["to"] >= 100:
                    result["limit_to"] += iter_limit
                    result["percentages"][-1]["limit"] += iter_limit
                else:
                    result["limit_from"] += iter_limit * percentage_from // 100
                    result["limit_to"] += iter_limit * percentage_to // 100
                    iter_result = {"limit": iter_limit, "from": percentage_from, "to": percentage_to}
                    result["percentages"].append(iter_result)

            if first_iter:
                first_iter = False

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
        result = FeedResult(
            data=[],
            next_page=FeedResultNextPage(
                data={
                    self.merger_id: FeedResultNextPageInside(
                        page=next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=False,
        )

        limits_and_percents = await self._calculate_limits_and_percents(
            page=result.next_page.data[self.merger_id].page,
            limit=limit,
        )

        dedup_active = bool(params.pop("_sf_dedup_active", False))

        from_priority = getattr(self.item_from.data, "dedup_priority", 0)
        to_priority = getattr(self.item_to.data, "dedup_priority", 0)

        if dedup_active and to_priority > from_priority:
            item_to = await self.item_to.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_to"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
            item_from = await self.item_from.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_from"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
        else:
            item_from = await self.item_from.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_from"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )
            item_to = await self.item_to.data.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limits_and_percents["limit_to"],
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )

        from_start_index = 0
        to_start_index = 0
        for lp_data in limits_and_percents["percentages"]:
            from_end_index = (lp_data["limit"] * lp_data["from"] // 100) + from_start_index
            to_end_index = (lp_data["limit"] * lp_data["to"] // 100) + to_start_index

            result.data.extend(item_from.data[from_start_index:from_end_index])
            result.data.extend(item_to.data[to_start_index:to_end_index])

            from_start_index = from_end_index
            to_start_index = to_end_index

        result.next_page.data.update(item_from.next_page.data)
        result.next_page.data.update(item_to.next_page.data)

        if any([item_from.has_next_page, item_to.has_next_page]):
            result.has_next_page = True

        if self.shuffle:
            shuffle(result.data)

        result.next_page.data[self.merger_id].page += 1

        return result
