from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import BaseModel, model_validator

from .base import BaseNode, coerce_feed_node
from ..execution.plans import MixChild, MixPlan


def _merge_cursor(child_cursors: Dict[str, dict]) -> dict:
    merged: dict = {}
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


class MergerPercentage(BaseNode):
    type: Literal["merger_percentage"] = "merger_percentage"
    node_id: str
    items: List[MergerPercentageItem]
    dedup_priority: int = 0

    def build_mix_plan(
        self,
        *,
        ctx: Any,
        limit: int,
        cursor: dict,
    ) -> MixPlan:
        total_pct = sum(item.percentage for item in self.items)

        # Compute per-child demand with remainder distribution
        demands: List[int] = []
        remainders: List[tuple] = []
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
            buffers: Dict[str, list],
            child_cursors: Dict[str, dict],
        ) -> Tuple[List[Any], dict]:
            # Simple concatenation: demand already ensures correct proportions
            merged_data: List[Any] = []
            for child in children:
                merged_data.extend(buffers.get(child.node_id, []))
            return merged_data, _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerAppend
# ---------------------------------------------------------------------------


class MergerAppend(BaseNode):
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
        ctx: Any,
        limit: int,
        cursor: dict,
    ) -> MixPlan:
        # Each child gets equal demand share; leftover goes to first children
        n = len(self.items)
        if n == 0:

            def assemble_empty(buffers: Dict[str, list], child_cursors: Dict[str, dict]) -> Tuple[List[Any], dict]:
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
            buffers: Dict[str, list],
            child_cursors: Dict[str, dict],
        ) -> Tuple[List[Any], dict]:
            merged_data: List[Any] = []
            for child in children:
                merged_data.extend(buffers.get(child.node_id, []))
            # Trim to limit in case of overfill
            return merged_data[:limit], _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)


# ---------------------------------------------------------------------------
# MergerPositional
# ---------------------------------------------------------------------------


class MergerPositional(BaseNode):
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
        ctx: Any,
        limit: int,
        cursor: dict,
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
            buffers: Dict[str, list],
            child_cursors: Dict[str, dict],
        ) -> Tuple[List[Any], dict]:
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


class MergerPercentageGradient(BaseNode):
    """Percentage-based merger that shifts the ratio over pages."""

    type: Literal["merger_percentage_gradient"] = "merger_percentage_gradient"
    node_id: str
    item_from: MergerPercentageItem
    item_to: MergerPercentageItem
    step: int
    size_to_step: int
    dedup_priority: int = 0

    def _calculate_demands(self, page: int, limit: int) -> tuple:
        percentage_from = self.item_from.percentage
        percentage_to = self.item_to.percentage
        start_position = limit * (page - 1)
        first_iter = True
        limit_from = 0
        limit_to = 0
        segments: List[Dict] = []

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
                from_take = iter_limit * percentage_from // 100
                to_take = iter_limit - from_take
                limit_from += from_take
                limit_to += to_take
                segments.append({"limit": iter_limit, "from_take": from_take, "to_take": to_take})

            if first_iter:
                first_iter = False

        return limit_from, limit_to, segments

    def build_mix_plan(
        self,
        *,
        ctx: Any,
        limit: int,
        cursor: dict,
    ) -> MixPlan:
        page = cursor.get(self.node_id, {}).get("page", 1)
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
            buffers: Dict[str, list],
            child_cursors: Dict[str, dict],
        ) -> Tuple[List[Any], dict]:
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


class MergerAppendDistribute(BaseNode):
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

    def _uniform_distribute(self, data: list) -> list:
        if self.sorting_key:
            data = sorted(data, key=lambda x: x[self.sorting_key], reverse=self.sorting_desc)

        grouped_entries: Dict[Any, deque] = defaultdict(deque)
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
        ctx: Any,
        limit: int,
        cursor: dict,
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
            buffers: Dict[str, list],
            child_cursors: Dict[str, dict],
        ) -> Tuple[List[Any], dict]:
            all_items: List[Any] = []
            for child in children:
                all_items.extend(buffers.get(child.node_id, []))
            distributed = self._uniform_distribute(all_items)
            return distributed[:limit], _merge_cursor(child_cursors)

        return MixPlan(children=children, assemble=assemble)
