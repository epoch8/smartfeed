from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Dict, List, Literal, cast

from ..execution.context import ExecutionContext
from ..execution.executor import SlotSpec, SlotsPlan
from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class MergerAppend(BaseFeedConfigModel):
    """Append merger."""

    merger_id: str
    type: Literal["merger_append"]
    items: List[FeedTypes]
    shuffle: bool = False

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> SlotsPlan:
        slots = [SlotSpec(owner=cast(BaseFeedConfigModel, item), max_count=limit) for item in self.items]

        def _assemble(
            output: List[Any], merged_next_page: FeedResultNextPage, owner_results: Dict[int, FeedResult]
        ) -> FeedResult:
            has_next_page = any(r.has_next_page for r in owner_results.values())
            result = FeedResult(data=output, next_page=merged_next_page, has_next_page=has_next_page)
            if self.shuffle:
                shuffle(result.data)
            return result

        return SlotsPlan(
            ctx=ctx,
            limit=limit,
            next_page=next_page,
            params=dict(params),
            slots=slots,
            assemble=_assemble,
        )
