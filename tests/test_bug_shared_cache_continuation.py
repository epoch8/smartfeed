"""BUG #2 -- shared cache (cache_key) cannot paginate past session_size.

When a cache_key wrapper exhausts its per-wrapper cache, the warm path calls
_cold_build with the continuation child_cursor, but _build_shared_base
(wrapper.py:536-539) fast-paths on the already-cached base and returns it,
ignoring the continuation cursor. Every "continuation" re-serves the first
session_size items under a fresh gen -> the feed loops on page 1 forever.

These tests fail today (non-termination / repeats) and must pass once the shared
base supports continuation.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


def _redis():
    return fakeredis.aioredis.FakeRedis()


def _shared_wrapper():
    return S.wrapper(S.subfeed("src", "src"), node_id="w",
                     session_size=20, cache_key="shared")


@pytest.mark.asyncio
async def test_shared_cache_paginates_to_exhaustion():
    src = S.ScriptedSource(S.unique_pool(100))
    node = _shared_wrapper()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    # Guard is low: a looping feed re-serves the same 20 items and never ends.
    pages = await S.drain(node, ctx, limit=10, max_pages=40)
    S.assert_no_duplicates(pages)
    S.assert_full_coverage(pages, set(range(100)))


@pytest.mark.asyncio
async def test_shared_cache_continuation_advances_past_session():
    """Page 3 (first page after the 20-item session is exhausted) must contain new
    ids, not repeat page 1."""
    src = S.ScriptedSource(S.unique_pool(100))
    node = _shared_wrapper()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    r3 = await run_executor.run(node, ctx, limit=10, cursor=r2.next_page)
    ids_seen = {it["id"] for it in r1.data} | {it["id"] for it in r2.data}
    ids_p3 = {it["id"] for it in r3.data}
    assert not (ids_seen & ids_p3), f"page 3 repeats earlier ids: {sorted(ids_seen & ids_p3)}"


@pytest.mark.asyncio
async def test_shared_cache_within_session_is_fine_control():
    """Control: within the first session_size items, shared cache paginates correctly."""
    src = S.ScriptedSource(S.unique_pool(100))
    node = _shared_wrapper()
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    ids = [it["id"] for it in r1.data] + [it["id"] for it in r2.data]
    assert ids == list(range(20))
