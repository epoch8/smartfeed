from __future__ import annotations

from collections import defaultdict, deque
from typing import TYPE_CHECKING, Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, model_validator

from ..execution.plans import MixChild, MixPlan
from .base import MixerNode, coerce_feed_node

if TYPE_CHECKING:
    from ..execution.context import ExecutionContext


def _merge_cursor(child_cursors: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    for c in child_cursors.values():
        merged.update(c)
    return merged


# ---------------------------------------------------------------------------
# MergerPercentageItem
# ---------------------------------------------------------------------------


class MergerPercentageItem(BaseModel):
    percentage: int
    data: Any  # BaseNode subclass

    @model_validator(mode="before")
    @classmethod
    def _coerce_data(cls, values: Any) -> Any:
        if isinstance(values, dict) and isinstance(values.get("data"), dict):
            values["data"] = coerce_feed_node(values["data"])
        return values


# ---------------------------------------------------------------------------
# MergerPercentage
# ---------------------------------------------------------------------------


class MergerPercentage(MixerNode):
    type: Literal["merger_percentage"] = "merger_percentage"
    node_id: str
    items: List[MergerPercentageItem]
    dedup_priority: int = 0

    def build_mix_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        cursor: Dict[str, Any],
    ) -> MixPlan:
        total_pct = sum(item.percentage for item in self.items)

        # Compute per-child demand with remainder distribution
        demands: List[int] = []
        remainders: List[Tuple[int, int]] = []
        for idx, item in enumerate(self.items):
            raw = limit * item.percentage
            child_limit = raw // 100
            demands.append(max(0, child_limit))
            remainders.append((raw % 100, idx))

        if total_pct == 100:
            missing = max(0, limit - sum(demands))
            if missing > 0:
                for _rem, idx in sorted(remainders, key=lambda x: (-x[0], x[1])):
                    if missing <= 0:
                        break
                    demands[idx] += 1
                    missing -= 1

        children = [
            MixChild(
                node_id=f"{self.node_id}_{idx}",
                node=item.data,
                demand=demands[idx],
            )
            for idx, item in enumerate(self.items)
        ]

        def assemble(
            buffers: Dict[str, List[Any]],
            child_cursors: Dict[str, Dict[str, Any]],
        ) -> Tuple[List[Any], Dict[str, Any]]:
            # Simple concatenation: demand already ensures correct proportions
            merged_data: List[Any] = []
            for child in children:
                merged_data.extend(buffers.get(child.node_id, []))
            return merged_data, _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerAppend
# ---------------------------------------------------------------------------


class MergerAppend(MixerNode):
    type: Literal["merger_append"] = "merger_append"
    node_id: str
    items: List[Any]  # list of BaseNode subclasses
    dedup_priority: int = 0

    @model_validator(mode="before")
    @classmethod
    def _coerce_items(cls, values: Any) -> Any:
        if isinstance(values, dict):
            raw_items = values.get("items")
            if isinstance(raw_items, list):
                values["items"] = [coerce_feed_node(item) if isinstance(item, dict) else item for item in raw_items]
        return values

    def build_mix_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        cursor: Dict[str, Any],
    ) -> MixPlan:
        # Each child gets equal demand share; leftover goes to first children
        n = len(self.items)
        if n == 0:

            def assemble_empty(
                buffers: Dict[str, List[Any]], child_cursors: Dict[str, Dict[str, Any]]
            ) -> Tuple[List[Any], Dict[str, Any]]:
                return [], _merge_cursor(child_cursors)

            return MixPlan(children=[], assemble=assemble_empty)

        base_demand = limit // n
        extra = limit - base_demand * n

        children = [
            MixChild(
                node_id=f"{self.node_id}_{idx}",
                node=item,
                demand=base_demand + (1 if idx < extra else 0),
            )
            for idx, item in enumerate(self.items)
        ]

        def assemble(
            buffers: Dict[str, List[Any]],
            child_cursors: Dict[str, Dict[str, Any]],
        ) -> Tuple[List[Any], Dict[str, Any]]:
            merged_data: List[Any] = []
            for child in children:
                merged_data.extend(buffers.get(child.node_id, []))
            # Trim to limit in case of overfill
            return merged_data[:limit], _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerPositional
# ---------------------------------------------------------------------------


class MergerPositional(MixerNode):
    type: Literal["merger_positional"] = "merger_positional"
    node_id: str
    positions: List[int] = []
    positional: Any  # BaseNode subclass
    default: Any  # BaseNode subclass
    dedup_priority: int = 0

    @model_validator(mode="before")
    @classmethod
    def _coerce_children(cls, values: Any) -> Any:
        if isinstance(values, dict):
            for field in ("positional", "default"):
                if isinstance(values.get(field), dict):
                    values[field] = coerce_feed_node(values[field])
        return values

    def build_mix_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        cursor: Dict[str, Any],
    ) -> MixPlan:
        # positions are 1-indexed; determine how many positional slots fall within [1..limit]
        pos_slots = [p for p in self.positions if 1 <= p <= limit]
        pos_count = len(pos_slots)
        default_count = limit - pos_count

        positional_child = MixChild(
            node_id=f"{self.node_id}_positional",
            node=self.positional,
            demand=pos_count,
        )
        default_child = MixChild(
            node_id=f"{self.node_id}_default",
            node=self.default,
            demand=default_count,
        )

        children = [positional_child, default_child]

        def assemble(
            buffers: Dict[str, List[Any]],
            child_cursors: Dict[str, Dict[str, Any]],
        ) -> Tuple[List[Any], Dict[str, Any]]:
            pos_items = deque(buffers.get(positional_child.node_id, []))
            def_items = deque(buffers.get(default_child.node_id, []))

            result: List[Any] = []
            pos_set = set(pos_slots)
            for position in range(1, limit + 1):
                if position in pos_set and pos_items:
                    result.append(pos_items.popleft())
                elif def_items:
                    result.append(def_items.popleft())
                elif pos_items:
                    result.append(pos_items.popleft())

            return result, _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerPercentageGradient
# ---------------------------------------------------------------------------


class MergerPercentageGradient(MixerNode):
    """Percentage-based merger that shifts the ratio over pages."""

    type: Literal["merger_percentage_gradient"] = "merger_percentage_gradient"
    node_id: str
    item_from: MergerPercentageItem
    item_to: MergerPercentageItem
    step: int = Field(gt=0)  # a zero/negative shift cannot progress (use merger_percentage for a fixed split)
    size_to_step: int = Field(gt=0)  # segment length; 0 would make the gradient walk undefined
    dedup_priority: int = 0

    def _percentages_at(self, k: int) -> Tuple[int, int]:
        """Percentage split at 1-indexed gradient segment k.

        Closed form of "shift by `step` once per segment while `to` < 100, clamping
        to (0, 100) on overshoot" -- so demand computation never has to replay the
        shift history from segment 1.
        """
        pct_from = self.item_from.percentage
        pct_to = self.item_to.percentage
        if k <= 1 or pct_to >= 100:
            return pct_from, pct_to
        # The walk freezes at the first shift where `to` reaches 100 (guard stops
        # shifting) or an overshoot clamps the pair to (0, 100).
        freeze = min(
            -((pct_to - 100) // self.step),  # ceil((100 - pct_to) / step): `to` reaches 100
            pct_from // self.step + 1,  # first shift where `from` would pass 0
        )
        shifts = min(k - 1, freeze)
        raw_from = pct_from - self.step * shifts
        raw_to = pct_to + self.step * shifts
        if raw_to > 100 or raw_from < 0:
            return 0, 100
        return raw_from, raw_to

    def _calculate_demands(self, page: int, limit: int) -> Tuple[int, int, List[Dict[str, int]]]:
        """Demands and interleave segments for THIS page's window only.

        Iterates just the gradient segments overlapping [limit*(page-1), limit*page)
        -- at most limit/size_to_step + 2 of them -- instead of replaying the whole
        scroll history, so per-request work is bounded by the page size no matter
        how deep (or how tampered) `page` is.
        """
        page_start = limit * (page - 1)
        page_end = limit * page
        limit_from = 0
        limit_to = 0
        segments: List[Dict[str, int]] = []

        first_seg = page_start // self.size_to_step + 1
        last_seg = -(-page_end // self.size_to_step)  # ceil
        for k in range(first_seg, last_seg + 1):
            lo = max(page_start, (k - 1) * self.size_to_step)
            hi = min(k * self.size_to_step, page_end)
            iter_limit = hi - lo
            if iter_limit <= 0:
                continue
            pct_from, _ = self._percentages_at(k)
            from_take = iter_limit * pct_from // 100
            to_take = iter_limit - from_take
            limit_from += from_take
            limit_to += to_take
            segments.append({"limit": iter_limit, "from_take": from_take, "to_take": to_take})

        return limit_from, limit_to, segments

    def build_mix_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        cursor: Dict[str, Any],
    ) -> MixPlan:
        node_cursor = cursor.get(self.node_id, {})
        if not isinstance(node_cursor, dict):
            raise ValueError(
                f"MergerPercentageGradient '{self.node_id}': cursor entry must be a dict, "
                f"got {type(node_cursor).__name__}"
            )
        page = node_cursor.get("page", 1)
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise ValueError(
                f"MergerPercentageGradient '{self.node_id}': cursor 'page' must be a positive int, got {page!r}"
            )
        limit_from, limit_to, segments = self._calculate_demands(page, limit)

        from_child = MixChild(
            node_id=f"{self.node_id}_from",
            node=self.item_from.data,
            demand=max(0, limit_from),
        )
        to_child = MixChild(
            node_id=f"{self.node_id}_to",
            node=self.item_to.data,
            demand=max(0, limit_to),
        )
        children = [from_child, to_child]

        def assemble(
            buffers: Dict[str, List[Any]],
            child_cursors: Dict[str, Dict[str, Any]],
        ) -> Tuple[List[Any], Dict[str, Any]]:
            from_data = list(buffers.get(from_child.node_id, []))
            to_data = list(buffers.get(to_child.node_id, []))
            result: List[Any] = []
            fi = ti = 0
            for seg in segments:
                ft = int(seg["from_take"])
                tt = int(seg["to_take"])
                result.extend(from_data[fi : fi + ft])
                result.extend(to_data[ti : ti + tt])
                fi += ft
                ti += tt
            merged_cur = _merge_cursor(child_cursors)
            merged_cur[self.node_id] = {"page": page + 1}
            return result, merged_cur

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerAppendDistribute
# ---------------------------------------------------------------------------


class MergerAppendDistribute(MixerNode):
    """Append merger that round-robins items by a distribution key."""

    type: Literal["merger_distribute"] = "merger_distribute"
    node_id: str
    items: List[Any]  # list of BaseNode subclasses
    distribution_key: str
    sorting_key: Optional[str] = None
    sorting_desc: bool = False
    dedup_priority: int = 0

    @model_validator(mode="before")
    @classmethod
    def _coerce_items(cls, values: Any) -> Any:
        if isinstance(values, dict):
            raw_items = values.get("items")
            if isinstance(raw_items, list):
                values["items"] = [coerce_feed_node(item) if isinstance(item, dict) else item for item in raw_items]
        return values

    def _uniform_distribute(self, data: List[Any]) -> List[Any]:
        if self.sorting_key:
            data = sorted(data, key=lambda x: x[self.sorting_key], reverse=self.sorting_desc)

        grouped_entries: Dict[Any, deque[Any]] = defaultdict(deque)
        for entry in data:
            grouped_entries[entry[self.distribution_key]].append(entry)

        result: List[Any] = []
        prev_key = None
        while any(grouped_entries.values()):
            for key in list(grouped_entries.keys()):
                if grouped_entries[key]:
                    if key != prev_key or len(grouped_entries) == 1:
                        result.append(grouped_entries[key].popleft())
                        prev_key = key
                    if not grouped_entries[key]:
                        del grouped_entries[key]
                else:
                    del grouped_entries[key]
        return result

    def build_mix_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        cursor: Dict[str, Any],
    ) -> MixPlan:
        children = [
            MixChild(
                node_id=f"{self.node_id}_{idx}",
                node=item,
                demand=limit,
            )
            for idx, item in enumerate(self.items)
        ]

        def assemble(
            buffers: Dict[str, List[Any]],
            child_cursors: Dict[str, Dict[str, Any]],
        ) -> Tuple[List[Any], Dict[str, Any]]:
            all_items: List[Any] = []
            for child in children:
                all_items.extend(buffers.get(child.node_id, []))
            distributed = self._uniform_distribute(all_items)
            return distributed[:limit], _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)
