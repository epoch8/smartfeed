from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage
from .context import ExecutionContext
from .cursors import CursorMap
from .plans import SlotsPlan

if TYPE_CHECKING:
    from .executor import Executor


class DedupRuntime:
    """Dedup/refill orchestration.

    This owns the control flow (refill loops, slot deficit refills, etc.)
    while `DeduplicationPolicy` stays focused on acceptance/arbitration decisions.
    """

    def __init__(self, executor: "Executor") -> None:
        self._executor = executor

    def _get_refill_settings(self, ctx: ExecutionContext) -> Any:
        return getattr(ctx, "refill_settings", None)

    async def run_node_with_dedup_refill(
        self,
        *,
        node: BaseFeedConfigModel,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        params: Dict[str, Any],
        initial_result: FeedResult,
    ) -> FeedResult:
        dedup = getattr(ctx, "dedup", None)
        if dedup is None:
            return initial_result

        settings = self._get_refill_settings(ctx)
        overfetch_factor = max(1, int(getattr(settings, "overfetch_factor", 1)))
        max_refill_loops = max(1, int(getattr(settings, "max_refill_loops", 20)))
        priority = int(getattr(node, "dedup_priority", 0))

        collected: List[Any] = []
        remaining = int(limit)
        loops = 0

        base_next_page = next_page
        current_result = initial_result
        request_limit = max(1, remaining)

        # NOTE: Refill loops are inherently sequential for a single node because
        # each subsequent request depends on the previous cursor.
        while remaining > 0:
            can_overfetch = CursorMap.can_overfetch(node=node, base_next_page=base_next_page)

            accepted, inspected_count = await dedup.accept_batch(
                items=list(current_result.data),
                priority=priority,
                limit=remaining,
            )

            if can_overfetch and request_limit > remaining:
                CursorMap.rewind_overfetch(
                    node=node,
                    base_next_page=base_next_page,
                    result_next_page=current_result.next_page,
                    inspected_count=inspected_count,
                    batch_size=len(current_result.data),
                )

            if accepted:
                collected.extend(accepted)
                remaining = limit - len(collected)

            if remaining <= 0 or not current_result.has_next_page or loops >= max_refill_loops:
                break
            loops += 1

            base_next_page = current_result.next_page
            request_limit = max(1, remaining)
            if CursorMap.can_overfetch(node=node, base_next_page=base_next_page) and overfetch_factor > 1:
                request_limit = max(1, remaining * overfetch_factor)

            current_result, _plan = await self._executor._run_node_raw(
                node,
                ctx,
                request_limit,
                base_next_page,
                params,
            )

        return FeedResult(
            data=collected,
            next_page=current_result.next_page,
            has_next_page=bool(current_result.has_next_page),
        )

    async def apply_slots_plan_dedup(
        self,
        *,
        plan: SlotsPlan,
        owners: List[Any],
        owner_index: Dict[int, int],
        owner_buffers: Dict[int, List[Any]],
        owner_results: Dict[int, FeedResult],
        dedup_policy: Any,
        refill_settings: Any,
        cursor: CursorMap,
    ) -> Tuple[Dict[int, List[Any]], Dict[int, FeedResult]]:
        owner_buffers = await dedup_policy.arbitrate_owner_buffers(
            owners=owners,
            owner_buffers=owner_buffers,
            owner_rank=owner_index,
        )

        for owner in owners:
            owner_id = id(owner)
            if owner_id not in owner_results:
                continue
            old = owner_results[owner_id]
            owner_results[owner_id] = FeedResult(
                data=list(owner_buffers.get(owner_id, [])),
                next_page=old.next_page,
                has_next_page=old.has_next_page,
            )

        deficits = self._compute_slot_deficits(plan=plan, owner_buffers=owner_buffers)
        if deficits:
            await self._refill_deficits(
                plan=plan,
                deficits=deficits,
                owners=owners,
                owner_index=owner_index,
                owner_buffers=owner_buffers,
                owner_results=owner_results,
                dedup_policy=dedup_policy,
                refill_settings=refill_settings,
                cursor=cursor,
            )

        return owner_buffers, owner_results

    def _compute_slot_deficits(self, *, plan: SlotsPlan, owner_buffers: Dict[int, List[Any]]) -> Dict[int, int]:
        quota_schedule = sum(int(s.max_count) for s in plan.slots) <= int(plan.limit)
        deficits: Dict[int, int] = {}
        consumed: Dict[int, int] = {}
        remaining = int(plan.limit)
        deficit_slots: List[int] = []

        for slot in plan.slots:
            if remaining <= 0:
                break

            owner_id = id(slot.owner)
            want = min(int(slot.max_count), remaining)
            if want <= 0:
                continue

            have_total = len(owner_buffers.get(owner_id, []))
            already = int(consumed.get(owner_id, 0))
            available = max(0, have_total - already)
            take = min(want, available)
            missing = max(0, want - take)
            if missing:
                deficit_slots.append(owner_id)
                if quota_schedule:
                    deficits[owner_id] = deficits.get(owner_id, 0) + missing
            consumed[owner_id] = already + take
            remaining -= take

        if quota_schedule:
            return deficits
        if remaining <= 0:
            return {}
        fallback_owner_id: Optional[int] = (
            deficit_slots[-1] if deficit_slots else (id(plan.slots[-1].owner) if plan.slots else None)
        )
        return {fallback_owner_id: remaining} if fallback_owner_id is not None else {}

    async def _refill_deficits(
        self,
        *,
        plan: SlotsPlan,
        deficits: Dict[int, int],
        owners: List[Any],
        owner_index: Dict[int, int],
        owner_buffers: Dict[int, List[Any]],
        owner_results: Dict[int, FeedResult],
        dedup_policy: Any,
        refill_settings: Any,
        cursor: CursorMap,
    ) -> None:
        overfetch_factor = max(1, int(getattr(refill_settings, "overfetch_factor", 1)))
        max_refill_loops = max(1, int(getattr(refill_settings, "max_refill_loops", 20)))

        deficit_owners: List[Any] = [o for o in owners if id(o) in deficits]
        deficit_owners = sorted(
            deficit_owners,
            key=lambda o: (
                int(getattr(o, "dedup_priority", 0)),
                owner_index.get(id(o), 0),
            ),
        )

        state: Dict[int, Dict[str, Any]] = {}
        for refill_owner in deficit_owners:
            refill_owner_id = id(refill_owner)
            missing_total = int(deficits.get(refill_owner_id, 0))
            if missing_total <= 0:
                continue

            base_np = owner_results[refill_owner_id].next_page if refill_owner_id in owner_results else plan.next_page
            state[refill_owner_id] = {
                "missing_total": missing_total,
                "remaining": missing_total,
                "accepted": [],
                "loops": 0,
                "current_next_page": base_np,
                "has_next_page": True,
            }

        if not state:
            return

        while True:
            wave_ops: List[Tuple[Any, int, FeedResultNextPage, int, bool]] = []
            for refill_owner in deficit_owners:
                refill_owner_id = id(refill_owner)
                owner_state = state.get(refill_owner_id)
                if owner_state is None:
                    continue
                if owner_state["remaining"] <= 0:
                    continue
                if not owner_state["has_next_page"]:
                    continue
                if owner_state["loops"] >= max_refill_loops:
                    continue

                base_np = owner_state["current_next_page"]
                remaining_before = max(1, int(owner_state["remaining"]))
                request_limit = remaining_before
                can_overfetch = CursorMap.can_overfetch(node=refill_owner, base_next_page=base_np)
                if can_overfetch and overfetch_factor > 1:
                    request_limit = max(1, remaining_before * overfetch_factor)

                wave_ops.append((refill_owner, refill_owner_id, base_np, request_limit, can_overfetch))

            if not wave_ops:
                break

            results = await self._executor.gather(
                *[
                    self._executor._run_owner(
                        plan=plan,
                        owner=owner,
                        demand=request_limit,
                        base_next_page=base_np,
                        dedup_active=True,
                    )
                    for owner, _owner_id, base_np, request_limit, _can_overfetch in wave_ops
                ]
            )

            for (owner, owner_id, base_np, request_limit, can_overfetch), result in zip(wave_ops, results):
                owner_state = state[owner_id]
                remaining_before = int(owner_state["remaining"])

                owner_state["current_next_page"] = result.next_page
                owner_state["has_next_page"] = bool(result.has_next_page)
                cursor.merge_delta(base_next_page=plan.next_page, owner_next_page=result.next_page)

                refill_prio = int(getattr(owner, "dedup_priority", 0))
                wave_accepted, inspected_count = await dedup_policy.accept_batch(
                    items=list(result.data),
                    priority=refill_prio,
                    limit=max(0, remaining_before),
                )

                if can_overfetch and request_limit > remaining_before:
                    CursorMap.rewind_overfetch(
                        node=owner,
                        base_next_page=base_np,
                        result_next_page=result.next_page,
                        inspected_count=inspected_count,
                        batch_size=len(result.data),
                    )

                if wave_accepted:
                    owner_state["accepted"].extend(wave_accepted)
                    owner_state["remaining"] = int(owner_state["missing_total"]) - len(owner_state["accepted"])

                if owner_state["remaining"] > 0 and owner_state["has_next_page"]:
                    owner_state["loops"] += 1

        for refill_owner in deficit_owners:
            refill_owner_id = id(refill_owner)
            owner_state = state.get(refill_owner_id)
            if owner_state is None:
                continue

            accepted = owner_state["accepted"]
            if accepted:
                owner_buffers.setdefault(refill_owner_id, [])
                owner_buffers[refill_owner_id].extend(accepted)

            owner_results[refill_owner_id] = FeedResult(
                data=list(owner_buffers.get(refill_owner_id, [])),
                next_page=owner_state["current_next_page"],
                has_next_page=owner_state["has_next_page"],
            )
