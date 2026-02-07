from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional, Tuple

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, _pydantic_deep_copy
from .context import ExecutionContext
from .cursors import CursorMap
from .plans import CallablePlan, Plan, SlotSpec, SlotsPlan


class Executor:
    """Shared execution engine.

    Owns recursion and concurrency. Nodes can optionally expose `build_plan(...)`.
    """

    async def run(
        self,
        node: BaseFeedConfigModel,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> FeedResult:
        result, plan = await self._run_node_raw(node, ctx, limit, next_page, params)

        dedup = getattr(ctx, "dedup", None)
        if dedup is None:
            return result

        if isinstance(plan, SlotsPlan):
            return result

        return await self._run_node_with_dedup_refill(node, ctx, limit, next_page, params, result)

    async def execute_plan(self, plan: Plan) -> FeedResult:
        """Interpret and execute a declarative plan.

        Plans must not perform execution themselves; they are data structures.
        """

        if isinstance(plan, SlotsPlan):
            return await self._execute_slots_plan(plan)
        if isinstance(plan, CallablePlan):
            return await plan.fn(self)
        raise TypeError(f"Unknown plan type: {type(plan)!r}")

    async def gather(self, *coros: Any) -> List[Any]:
        """Execute coroutines concurrently.

        Centralizes concurrency in the executor layer.
        """

        return list(await asyncio.gather(*coros))

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _run_node_raw(
        self,
        node: BaseFeedConfigModel,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        params: Dict[str, Any],
    ) -> Tuple[FeedResult, Optional[Plan]]:
        build_plan = getattr(node, "build_plan", None)
        if callable(build_plan):
            plan: Plan = build_plan(ctx=ctx, limit=limit, next_page=next_page, **params)
            result = await self.execute_plan(plan)
            return result, plan

        result = await node.get_data(
            methods_dict=ctx.methods_dict,
            user_id=ctx.user_id,
            limit=limit,
            next_page=next_page,
            redis_client=ctx.redis_client,
            ctx=ctx,
            **params,
        )
        return result, None

    async def _execute_slots_plan(self, plan: SlotsPlan) -> FeedResult:
        if plan.limit <= 0:
            assembled = await self._maybe_await(plan.assemble([], plan.next_page, {}))
            return assembled

        working_next_page = _pydantic_deep_copy(plan.next_page)
        cursor = CursorMap(working_next_page)
        owners, owner_index = self._collect_plan_owners(plan)
        dedup_policy = getattr(plan.ctx, "dedup", None)
        refill_settings = getattr(plan.ctx, "refill_settings", None) or getattr(plan.ctx, "dedup_settings", None)
        dedup_active = dedup_policy is not None

        owner_max_demand = self._owner_slot_demand(plan)
        owner_buffers, owner_results = await self._run_plan_owners(
            plan=plan,
            owners=owners,
            owner_max_demand=owner_max_demand,
            dedup_active=dedup_active,
            cursor=cursor,
        )

        if dedup_policy is not None:
            owner_buffers, owner_results = await self._arbitrate_owner_buffers(
                owners=owners,
                owner_index=owner_index,
                owner_buffers=owner_buffers,
                owner_results=owner_results,
                dedup_policy=dedup_policy,
            )

            deficits = self._compute_slot_deficits(
                plan=plan,
                owner_buffers=owner_buffers,
            )
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

        output = self._consume_slots(plan=plan, owner_buffers=owner_buffers)
        assembled = await self._maybe_await(plan.assemble(output, cursor.next_page, owner_results))
        return assembled

    def _owner_slot_demand(self, plan: SlotsPlan) -> Dict[int, int]:
        """Compute a per-owner maximum demand based on the slot schedule."""

        demand: Dict[int, int] = {}
        for slot in plan.slots:
            owner_id = id(slot.owner)
            demand[owner_id] = demand.get(owner_id, 0) + int(slot.max_count)
        return demand

    def _collect_plan_owners(self, plan: SlotsPlan) -> tuple[List[Any], Dict[int, int]]:
        owners: List[Any] = []
        owner_index: Dict[int, int] = {}
        for slot in plan.slots:
            owner_id = id(slot.owner)
            if owner_id in owner_index:
                continue
            owner_index[owner_id] = len(owners)
            owners.append(slot.owner)
        return owners, owner_index

    async def _run_owner(
        self,
        *,
        plan: SlotsPlan,
        owner: Any,
        demand: int,
        base_next_page: FeedResultNextPage,
        dedup_active: bool,
    ) -> FeedResult:
        isolated_next_page = _pydantic_deep_copy(base_next_page)
        owner_ctx = plan.ctx
        if dedup_active:
            owner_ctx = ExecutionContext(
                methods_dict=plan.ctx.methods_dict,
                user_id=plan.ctx.user_id,
                redis_client=plan.ctx.redis_client,
                executor=plan.ctx.executor,
                dedup=None,
                refill_settings=None,
                dedup_settings=None,
            )
        return await self.run(owner, owner_ctx, demand, isolated_next_page, **plan.params)

    async def _run_plan_owners(
        self,
        *,
        plan: SlotsPlan,
        owners: List[Any],
        owner_max_demand: Dict[int, int],
        dedup_active: bool,
        cursor: CursorMap,
    ) -> tuple[Dict[int, List[Any]], Dict[int, FeedResult]]:
        owner_buffers: Dict[int, List[Any]] = {id(o): [] for o in owners}
        owner_results: Dict[int, FeedResult] = {}

        ops: List[tuple[Any, int]] = []
        for owner in owners:
            if plan.owner_fetch_limits is not None and id(owner) in plan.owner_fetch_limits:
                demand = int(plan.owner_fetch_limits[id(owner)])
            else:
                demand = min(plan.limit, int(owner_max_demand.get(id(owner), 0)))
            if demand > 0:
                ops.append((owner, demand))

        if not ops:
            return owner_buffers, owner_results

        results = await self.gather(
            *[
                self._run_owner(
                    plan=plan,
                    owner=owner,
                    demand=demand,
                    base_next_page=plan.next_page,
                    dedup_active=dedup_active,
                )
                for owner, demand in ops
            ]
        )
        for (owner, _demand), owner_result in zip(ops, results):
            owner_results[id(owner)] = owner_result
            owner_buffers[id(owner)] = list(owner_result.data)
            cursor.merge_delta(
                base_next_page=plan.next_page,
                owner_next_page=owner_result.next_page,
            )

        return owner_buffers, owner_results

    async def _arbitrate_owner_buffers(
        self,
        *,
        owners: List[Any],
        owner_index: Dict[int, int],
        owner_buffers: Dict[int, List[Any]],
        owner_results: Dict[int, FeedResult],
        dedup_policy: Any,
    ) -> tuple[Dict[int, List[Any]], Dict[int, FeedResult]]:
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

        return owner_buffers, owner_results

    def _compute_slot_deficits(
        self,
        *,
        plan: SlotsPlan,
        owner_buffers: Dict[int, List[Any]],
    ) -> Dict[int, int]:
        total_max = sum(int(s.max_count) for s in plan.slots)
        quota_schedule = total_max <= int(plan.limit)

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
            if take < want:
                deficit_slots.append(owner_id)
            consumed[owner_id] = already + take
            remaining -= take

        page_underfilled = remaining > 0

        if quota_schedule:
            return self._compute_quota_deficits(plan=plan, owner_buffers=owner_buffers)
        if not page_underfilled:
            return {}
        return self._compute_fill_deficits(plan=plan, remaining=remaining, deficit_slots=deficit_slots)

    def _compute_quota_deficits(
        self,
        *,
        plan: SlotsPlan,
        owner_buffers: Dict[int, List[Any]],
    ) -> Dict[int, int]:
        deficits: Dict[int, int] = {}
        remaining = int(plan.limit)
        consumed: Dict[int, int] = {}
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
                deficits[owner_id] = deficits.get(owner_id, 0) + missing
            consumed[owner_id] = already + take
            remaining -= take

        return deficits

    def _compute_fill_deficits(
        self,
        *,
        plan: SlotsPlan,
        remaining: int,
        deficit_slots: List[int],
    ) -> Dict[int, int]:
        to_fill = int(remaining)
        if to_fill <= 0:
            return {}

        owner_id = deficit_slots[-1] if deficit_slots else (id(plan.slots[-1].owner) if plan.slots else None)
        return {owner_id: to_fill} if owner_id is not None else {}

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
                "owner": refill_owner,
                "missing_total": missing_total,
                "remaining": int(missing_total),
                "accepted": [],
                "loops": 0,
                "current_next_page": base_np,
                "has_next_page": True,
                "last_result": None,
                "last_request_limit": 0,
                "last_can_overfetch": False,
                "last_base_next_page": base_np,
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

            results = await self.gather(
                *[
                    self._run_owner(
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

    def _consume_slots(self, *, plan: SlotsPlan, owner_buffers: Dict[int, List[Any]]) -> List[Any]:
        output: List[Any] = []
        for slot in plan.slots:
            if len(output) >= plan.limit:
                break

            remaining = plan.limit - len(output)
            take = min(int(slot.max_count), remaining)
            if take <= 0:
                continue

            owner_buffer = owner_buffers.get(id(slot.owner), [])
            if not owner_buffer:
                continue

            chunk = owner_buffer[:take]
            del owner_buffer[: len(chunk)]
            output.extend(chunk)

        return output

    async def _run_node_with_dedup_refill(
        self,
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

        settings = getattr(ctx, "refill_settings", None) or getattr(ctx, "dedup_settings", None)
        overfetch_factor = max(1, int(getattr(settings, "overfetch_factor", 1)))
        max_refill_loops = max(1, int(getattr(settings, "max_refill_loops", 20)))
        priority = int(getattr(node, "dedup_priority", 0))

        collected: List[Any] = []
        remaining = int(limit)
        loops = 0

        current_result = initial_result
        current_next_page = current_result.next_page
        current_request_limit = max(1, remaining)
        has_next_page = bool(current_result.has_next_page)
        base_next_page = next_page

        while remaining > 0:
            can_overfetch = CursorMap.can_overfetch(node=node, base_next_page=base_next_page)

            accepted, inspected_count = await dedup.accept_batch(
                items=list(current_result.data),
                priority=priority,
                limit=remaining,
            )

            if can_overfetch and current_request_limit > remaining:
                CursorMap.rewind_overfetch(
                    node=node,
                    base_next_page=base_next_page,
                    result_next_page=current_next_page,
                    inspected_count=inspected_count,
                    batch_size=len(current_result.data),
                )

            if accepted:
                collected.extend(accepted)
                remaining = limit - len(collected)

            if remaining <= 0 or not has_next_page or loops >= max_refill_loops:
                break
            loops += 1

            base_next_page = current_next_page
            next_request_limit = max(1, remaining)
            can_overfetch = CursorMap.can_overfetch(node=node, base_next_page=base_next_page)
            if can_overfetch and overfetch_factor > 1:
                next_request_limit = max(1, remaining * overfetch_factor)

            current_result, _plan = await self._run_node_raw(
                node,
                ctx,
                next_request_limit,
                base_next_page,
                params,
            )
            current_next_page = current_result.next_page
            current_request_limit = next_request_limit
            has_next_page = bool(current_result.has_next_page)

        return FeedResult(
            data=collected,
            next_page=current_next_page,
            has_next_page=has_next_page,
        )


__all__ = [
    "Executor",
    "Plan",
    "CallablePlan",
    "SlotSpec",
    "SlotsPlan",
]
