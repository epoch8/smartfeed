from __future__ import annotations

import asyncio
from typing import Any

from smartfeed.models.base import BaseNode, FeedResult
from .context import ExecutionContext
from .plans import MixPlan


async def run(node: BaseNode, ctx: ExecutionContext, limit: int, cursor: dict) -> FeedResult:
    """Execute a feed node. Dispatches to node.execute() or build_mix_plan()."""
    if hasattr(node, "execute"):
        return await node.execute(
            methods_dict=ctx.methods_dict,
            session_id=ctx.session_id,
            limit=limit,
            cursor=cursor,
            ctx=ctx,
        )

    plan: MixPlan = node.build_mix_plan(ctx=ctx, limit=limit, cursor=cursor)
    return await _execute_mix(plan, ctx, cursor)


async def _execute_mix(plan: MixPlan, ctx: ExecutionContext, cursor: dict) -> FeedResult:
    """Execute a MixPlan: run children in parallel, assemble results."""
    if not plan.children:
        merged, merged_cursor = plan.assemble({}, {})
        return FeedResult(data=merged, next_page=merged_cursor, has_next_page=False)

    tasks = [run(c.node, ctx, c.demand, cursor) for c in plan.children]
    results = await asyncio.gather(*tasks)

    buffers = {}
    child_cursors = {}
    any_has_next = False
    for child, result in zip(plan.children, results):
        buffers[child.node_id] = result.data
        child_cursors[child.node_id] = result.next_page
        any_has_next = any_has_next or result.has_next_page

    merged, merged_cursor = plan.assemble(buffers, child_cursors)
    return FeedResult(data=merged, next_page=merged_cursor, has_next_page=any_has_next)
