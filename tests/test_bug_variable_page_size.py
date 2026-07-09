"""Regression: changing `limit` between pages on a cached wrapper skips/repeats.

The cursor stores a page *number*; the offset is recomputed as (page-1)*limit at
read time (_paginate, wrapper.py:343). If the client changes page size between
requests, the page-number -> offset mapping is inconsistent and items are
skipped (growing limit) or repeated (shrinking limit).

Fails today; passes once the cursor tracks an absolute offset (or page size is
pinned into the cursor).
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


def _redis():
    return fakeredis.aioredis.FakeRedis()


def _cached():
    # session_size large enough that both pages come from one cached batch.
    return S.wrapper(S.subfeed("src", "src"), session_size=200)


@pytest.mark.asyncio
async def test_growing_limit_does_not_skip():
    src = S.ScriptedSource(S.unique_pool(200))
    node = _cached()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})  # expect ids 0..9
    r2 = await run_executor.run(node, ctx, limit=50, cursor=r1.next_page)  # expect 10..59
    served = [it["id"] for it in r1.data] + [it["id"] for it in r2.data]
    assert served == list(range(60)), f"expected contiguous 0..59, got gaps: {served[:15]}..."


@pytest.mark.asyncio
async def test_shrinking_limit_does_not_repeat():
    src = S.ScriptedSource(S.unique_pool(200))
    node = _cached()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=50, cursor={})  # ids 0..49
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)  # expect 50..59
    served = [it["id"] for it in r1.data] + [it["id"] for it in r2.data]
    assert len(served) == len(
        set(served)
    ), f"repeated ids on shrink: {sorted({x for x in served if served.count(x) > 1})}"


@pytest.mark.asyncio
async def test_constant_limit_is_fine_control():
    """Control: with a constant limit the page cursor works correctly."""
    src = S.ScriptedSource(S.unique_pool(200))
    node = _cached()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    served = [it["id"] for it in r1.data] + [it["id"] for it in r2.data]
    assert served == list(range(20))
