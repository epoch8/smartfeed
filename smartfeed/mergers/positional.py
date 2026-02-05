from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Dict, List, Literal, Optional, Union

import redis
from pydantic import model_validator
from redis.asyncio import Redis as AsyncRedis

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
        **params: Any,
    ) -> FeedResult:
        dedup_active = bool(params.pop("_sf_dedup_active", False))

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

        if dedup_active and getattr(self.positional, "dedup_priority", 0) > getattr(self.default, "dedup_priority", 0):
            pos_res = await self.positional.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=len(page_positions),
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
            default_res = await self.default.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=True,
                **params,
            )
        else:
            default_res = await self.default.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=limit,
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )
            pos_res = await self.positional.get_data(
                methods_dict=methods_dict,
                user_id=user_id,
                limit=len(page_positions),
                next_page=next_page,
                redis_client=redis_client,
                _sf_dedup_active=dedup_active,
                **params,
            )

        result = FeedResult(
            data=default_res.data,
            next_page=FeedResultNextPage(
                data={
                    self.merger_id: FeedResultNextPageInside(
                        page=page,
                        after=next_page.data[self.merger_id].after if self.merger_id in next_page.data else None,
                    )
                },
            ),
            has_next_page=default_res.has_next_page,
        )

        if not result.has_next_page and all([positional_has_next_page, pos_res.has_next_page]):
            result.has_next_page = True

        result.next_page.data.update(default_res.next_page.data)
        result.next_page.data.update(pos_res.next_page.data)

        for i, post in enumerate(pos_res.data):
            result.data = result.data[: page_positions[i] - 1] + [post] + result.data[page_positions[i] - 1 :]

        if len(result.data) > limit:
            result.data = result.data[:limit]

        result.next_page.data[self.merger_id].page += 1

        return result
