from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional
from typing_extensions import no_type_check

from ..execution.context import ExecutionContext
from ..execution.executor import SlotSpec, SlotsPlan
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
    def _uniform_distribute(self, data: list) -> list:
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

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> SlotsPlan:
        slots = [SlotSpec(owner=item, max_count=limit) for item in self.items]

        def _assemble(
            output: List[Any], merged_next_page: FeedResultNextPage, owner_results: Dict[int, FeedResult]
        ) -> FeedResult:
            has_next_page = any(r.has_next_page for r in owner_results.values())
            distributed = self._uniform_distribute(output)
            return FeedResult(data=distributed, next_page=merged_next_page, has_next_page=has_next_page)

        return SlotsPlan(
            ctx=ctx,
            limit=limit,
            next_page=next_page,
            params=dict(params),
            slots=slots,
            assemble=_assemble,
        )
