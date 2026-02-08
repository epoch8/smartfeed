from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, List, Optional, Tuple

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage, _pydantic_deep_copy
from .context import ExecutionContext
from .cursors import CursorMap
from .dedup_runtime import DedupRuntime
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

        return await self._dedup_runtime().run_node_with_dedup_refill(
            node=node,
            ctx=ctx,
            limit=limit,
            next_page=next_page,
            params=params,
            initial_result=result,
        )

    def _dedup_runtime(self) -> DedupRuntime:
        runtime = getattr(self, "_dedup_runtime_instance", None)
        if runtime is None:
            runtime = DedupRuntime(self)
            setattr(self, "_dedup_runtime_instance", runtime)
        return runtime

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
        owners, owner_index, owner_max_demand = self._collect_plan_owners(plan)
        dedup_policy = getattr(plan.ctx, "dedup", None)
        refill_settings = getattr(plan.ctx, "refill_settings", None)
        dedup_active = dedup_policy is not None

        owner_buffers, owner_results = await self._run_plan_owners(
            plan=plan,
            owners=owners,
            owner_max_demand=owner_max_demand,
            dedup_active=dedup_active,
            cursor=cursor,
        )

        if dedup_policy is not None:
            owner_buffers, owner_results = await self._dedup_runtime().apply_slots_plan_dedup(
                plan=plan,
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

    def _collect_plan_owners(self, plan: SlotsPlan) -> tuple[List[Any], Dict[int, int], Dict[int, int]]:
        owners: List[Any] = []
        owner_index: Dict[int, int] = {}
        owner_demand: Dict[int, int] = {}
        for slot in plan.slots:
            owner_id = id(slot.owner)
            if owner_id not in owner_index:
                owner_index[owner_id] = len(owners)
                owners.append(slot.owner)
            owner_demand[owner_id] = owner_demand.get(owner_id, 0) + int(slot.max_count)
        return owners, owner_index, owner_demand

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


__all__ = [
    "Executor",
    "Plan",
    "CallablePlan",
    "SlotSpec",
    "SlotsPlan",
]
