from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, cast

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
    def _merge_items_data(items_data: List[List], weights: Optional[List[int]] = None) -> List:
        """Interleave sources keeping the TARGET ratio from the front.

        Uses a smooth weighted round-robin: at each step the source that is most
        "behind" its target share (lowest emitted/weight) and still has items is
        emitted next. This front-loads the configured percentages -- a scarce
        source keeps its target share on the early output and then drops out,
        instead of being smeared thinly across the whole result.
        """
        n = len(items_data)
        if weights is None or len(weights) != n:
            weights = [1] * n
        safe_weights = [w if isinstance(w, int) and w > 0 else 1 for w in weights]

        pointers = [0] * n
        emitted = [0] * n
        total = sum(len(item_data) for item_data in items_data)
        result: List = []

        while len(result) < total:
            best = -1
            best_score: Optional[float] = None
            for i in range(n):
                if pointers[i] >= len(items_data[i]):
                    continue
                score = emitted[i] / safe_weights[i]
                if best_score is None or score < best_score:
                    best, best_score = i, score
            if best < 0:
                break
            result.append(items_data[best][pointers[best]])
            pointers[best] += 1
            emitted[best] += 1

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

            weights = [int(item.percentage) for item in self.items]
            data = self._merge_items_data(items_data=items_data, weights=weights)
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
