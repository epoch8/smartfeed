"""Pagination edge cases: has_next_page exactness at exhaustion, empty sources,
limit larger than the pool, and shuffle interacting with cross-page dedup.

These are green controls -- they pin correct behavior that a regression could
break (a phantom trailing empty page, a lost final item, shuffle defeating the
seen-set).
"""

import math

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution import executor as run_executor


def _redis():
    return fakeredis.aioredis.FakeRedis()


# ---------------------------------------------------------------------------
# has_next_page exactness: no phantom trailing empty page, no lost final item
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("n,limit", [(25, 10), (30, 10), (20, 10), (40, 10), (5, 10), (1, 10)])
async def test_has_next_exact_passthrough(n, limit):
    src = S.ScriptedSource(S.unique_pool(n))
    node = S.wrapper(S.subfeed("src", "src"))  # passthrough, no cache
    ctx = S.make_ctx({"src": src}, redis=None)
    pages = await S.drain(node, ctx, limit=limit, max_pages=50)
    assert sum(len(p) for p in pages) == n, "not every item was served"
    assert all(len(p) > 0 for p in pages), "phantom trailing empty page"
    assert len(pages) == max(1, math.ceil(n / limit))


@pytest.mark.asyncio
@pytest.mark.parametrize("n,limit", [(25, 10), (30, 10), (20, 10), (45, 10)])
async def test_has_next_exact_cached(n, limit):
    src = S.ScriptedSource(S.unique_pool(n))
    node = S.wrapper(S.subfeed("src", "src"), session_size=20)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=limit, max_pages=50)
    assert sum(len(p) for p in pages) == n
    assert all(len(p) > 0 for p in pages), "phantom trailing empty page"


# ---------------------------------------------------------------------------
# Empty source
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_empty_source_returns_empty_no_next():
    src = S.ScriptedSource([])
    node = S.wrapper(S.subfeed("src", "src"), session_size=20, dedup_key="id", overfetch_factor=4)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert r.data == []
    assert r.has_next_page is False


# ---------------------------------------------------------------------------
# limit larger than the whole pool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_limit_larger_than_pool():
    src = S.ScriptedSource(S.unique_pool(5))
    node = S.wrapper(S.subfeed("src", "src"))
    ctx = S.make_ctx({"src": src}, redis=None)
    pages = await S.drain(node, ctx, limit=10, max_pages=10)
    assert [it["id"] for pg in pages for it in pg] == list(range(5))


# ---------------------------------------------------------------------------
# shuffle=True must not defeat cross-page dedup (seen-set)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_shuffle_does_not_defeat_cross_page_dedup():
    pool = S.unique_pool(30)
    pool.insert(25, {"id": 3, "val": "again"})  # id 3 re-emitted on a later page
    src = S.ScriptedSource(pool)
    node = S.wrapper(SubFeed(subfeed_id="src", method_name="src", shuffle=True),
                     dedup_key="id", overfetch_factor=1)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=5, max_pages=50)
    ids = S.flat_ids(pages)
    assert ids.count(3) == 1, "shuffle let a cross-page duplicate through the seen-set"
    S.assert_full_coverage(pages, set(range(30)))
