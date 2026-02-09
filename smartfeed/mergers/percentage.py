from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Dict, List, Literal, cast

from pydantic import BaseModel

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

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> SlotsPlan:
        owners: List[BaseFeedConfigModel] = [cast(BaseFeedConfigModel, item.data) for item in self.items]

        slot_limits: List[int] = []
        remainders: List[tuple[int, int]] = []
        total_percentage = sum(int(item.percentage) for item in self.items)

        for idx, item in enumerate(self.items):
            raw = int(limit) * int(item.percentage)
            child_limit = raw // 100
            slot_limits.append(max(0, child_limit))
            remainders.append((raw % 100, idx))

        # avoid underfilling for the common "percentages sum to 100" case
        if total_percentage == 100:
            missing = max(0, int(limit) - sum(slot_limits))
            if missing > 0:
                for _rem, idx in sorted(remainders, key=lambda x: (-x[0], x[1])):
                    if missing <= 0:
                        break
                    slot_limits[idx] += 1
                    missing -= 1

        slots: List[SlotSpec] = [
            SlotSpec(owner=owner, max_count=max(0, int(slot_limits[idx]))) for idx, owner in enumerate(owners)
        ]

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
