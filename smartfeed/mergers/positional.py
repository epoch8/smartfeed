from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union

import redis
from pydantic import model_validator
from redis.asyncio import Redis as AsyncRedis

from ..execution.context import ExecutionContext
from ..execution.executor import SlotSpec, SlotsPlan
from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, FeedResultNextPageInside

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerPositional(BaseFeedConfigModel):
    """Positional merger."""

    merger_id: str
    type: Literal["merger_positional"]
    positions: List[int] = []
    start: Optional[int] = None
    end: Optional[int] = None
    step: Optional[int] = None
    positional: FeedTypes
    default: FeedTypes

    @model_validator(mode="after")
    def validate_merger_positional(self) -> "MergerPositional":
        if not self.positions and not all((self.start, self.end, self.step)):
            raise ValueError('Either "positions" or "start", "end", and "step" must be provided')
        if self.start and self.positions:
            if isinstance(self.start, int) and self.start <= max(self.positions):
                raise ValueError('"start" must be bigger than maximum value of "positions"')
        if isinstance(self.start, int) and isinstance(self.end, int):
            if self.end <= self.start:
                raise ValueError('"end" must be bigger than "start"')
        return self

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
        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1

        positional_has_next_page = True
        page_positions: List[int] = []
        available_positions = range((page - 1) * limit, (page * limit) + 1)
        for position in self.positions:
            if position in available_positions:
                page_positions.append(available_positions.index(position))

        if max(available_positions) >= max(self.positions, default=0):
            positional_has_next_page = False

        if self.start is not None and self.end is not None and self.step is not None:
            positional_has_next_page = not max(available_positions) >= self.end

            for position in range(self.start, self.end, self.step):
                if position in available_positions:
                    page_positions.append(available_positions.index(position))

        pos_limit = len(page_positions)

        # Build a slot ownership schedule by applying the same sequential insert
        # semantics as the legacy assembly logic.
        schedule: List[BaseFeedConfigModel] = [self.default for _ in range(limit)]
        for insert_index in [p - 1 for p in page_positions[:pos_limit]]:
            schedule.insert(insert_index, self.positional)
        schedule = schedule[:limit]

        # Compress the schedule into contiguous segments.
        slots: List[SlotSpec] = []
        if schedule:
            current_owner = schedule[0]
            count = 1
            for owner in schedule[1:]:
                if owner is current_owner:
                    count += 1
                    continue
                slots.append(SlotSpec(owner=current_owner, max_count=count))
                current_owner = owner
                count = 1
            slots.append(SlotSpec(owner=current_owner, max_count=count))

        after = next_page.data[self.merger_id].after if self.merger_id in next_page.data else None

        def _assemble(
            output: List[Any], merged_next_page: FeedResultNextPage, owner_results: Dict[int, FeedResult]
        ) -> FeedResult:
            default_res = owner_results.get(id(self.default))
            pos_res = owner_results.get(id(self.positional))

            has_next_page = bool(default_res.has_next_page) if default_res is not None else False
            if not has_next_page and positional_has_next_page and pos_res is not None and pos_res.has_next_page:
                has_next_page = True

            result_next_page = merged_next_page
            result_next_page.data[self.merger_id] = FeedResultNextPageInside(page=page + 1, after=after)
            return FeedResult(data=output, next_page=result_next_page, has_next_page=has_next_page)

        return SlotsPlan(
            ctx=ctx,
            limit=limit,
            next_page=next_page,
            params=dict(params),
            slots=slots,
            owner_fetch_limits={
                id(self.default): limit,
                id(self.positional): pos_limit,
            },
            assemble=_assemble,
        )
