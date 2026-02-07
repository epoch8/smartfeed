from random import shuffle
from typing import Any, Callable, Dict, List, Literal, Optional, Union, cast

import redis
from pydantic import model_validator
from redis.asyncio import Redis as AsyncRedis

from ..execution.context import ExecutionContext
from ..execution.executor import SlotSpec, SlotsPlan
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

    def _calculate_limits_and_percents(self, page: int, limit: int) -> Dict:
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
        ctx: Optional[ExecutionContext] = None,
        **params: Any,
    ) -> FeedResult:
        if ctx is None:
            ctx = ExecutionContext(methods_dict=methods_dict, user_id=user_id, redis_client=redis_client)

        if ctx.executor is None:
            from ..execution.executor import Executor

            ctx.executor = Executor()

        return await ctx.executor.run(self, ctx, limit, next_page, **params)

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> SlotsPlan:
        start_page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        start_after = next_page.data[self.merger_id].after if self.merger_id in next_page.data else None

        plan_next_page = FeedResultNextPage(
            data={
                **next_page.data,
                self.merger_id: FeedResultNextPageInside(page=start_page, after=start_after),
            }
        )

        limits_and_percents = self._calculate_limits_and_percents(page=start_page, limit=limit)

        owner_from = cast(BaseFeedConfigModel, self.item_from.data)
        owner_to = cast(BaseFeedConfigModel, self.item_to.data)

        slots = [
            SlotSpec(owner=owner_from, max_count=int(limits_and_percents["limit_from"])),
            SlotSpec(owner=owner_to, max_count=int(limits_and_percents["limit_to"])),
        ]

        async def _assemble(
            output: List[Any],
            merged_next_page: FeedResultNextPage,
            owner_results: Dict[int, FeedResult],
        ) -> FeedResult:
            from_res = owner_results.get(id(owner_from))
            to_res = owner_results.get(id(owner_to))

            from_data = list(from_res.data) if from_res is not None else []
            to_data = list(to_res.data) if to_res is not None else []

            data: List[Any] = []
            from_start_index = 0
            to_start_index = 0
            for lp_data in limits_and_percents["percentages"]:
                from_end_index = (lp_data["limit"] * lp_data["from"] // 100) + from_start_index
                to_end_index = (lp_data["limit"] * lp_data["to"] // 100) + to_start_index

                data.extend(from_data[from_start_index:from_end_index])
                data.extend(to_data[to_start_index:to_end_index])

                from_start_index = from_end_index
                to_start_index = to_end_index

            has_next_page = False
            if from_res is not None and from_res.has_next_page:
                has_next_page = True
            if to_res is not None and to_res.has_next_page:
                has_next_page = True

            if self.shuffle:
                shuffle(data)

            if self.merger_id in merged_next_page.data:
                merged_next_page.data[self.merger_id].page += 1

            return FeedResult(data=data, next_page=merged_next_page, has_next_page=has_next_page)

        return SlotsPlan(
            ctx=ctx,
            limit=limit,
            next_page=plan_next_page,
            params=dict(params),
            slots=slots,
            assemble=_assemble,
        )
