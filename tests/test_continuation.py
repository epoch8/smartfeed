"""Tests for Wrapper cache exhaustion and continuation cursor rebuild.

Scenario:
  Wrapper(cache, session_size=20) + limit=10.
  - Page 1: cold build -> fetches 20 items, caches them, returns items 0-9
  - Page 2: warm hit  -> returns items 10-19
  - Page 3: cache exhausted (start=20 >= 20 cached) -> cold rebuild from
            child_cursor stored in meta; new generation spawned.

The child source tracks its own page via next_page so the rebuild continues
from item 20 onwards with no gap and no overlap.
"""

import pytest
import fakeredis.aioredis

from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.wrapper import Wrapper, WrapperCache
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


# ---------------------------------------------------------------------------
# Infinite stateful source: returns items based on page cursor
# ---------------------------------------------------------------------------

async def infinite_source(user_id, limit, next_page, **kw):
    """Returns `limit` items starting from (page-1)*limit.  IDs are globally unique."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": start + i, "val": f"item_{start + i}"} for i in range(limit)]
    return FeedResult(
        data=data,
        next_page={"page": page + 1},
        has_next_page=True,
    )


METHODS = {"source": infinite_source}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="test_session", methods_dict=METHODS, redis=redis)


def _make_wrapper(session_size=20):
    return Wrapper(
        node_id="w",
        cache=WrapperCache(session_size=session_size, session_ttl=300),
        data=SubFeed(subfeed_id="src", method_name="source"),
    )


# ---------------------------------------------------------------------------
# Helper: collect three pages
# ---------------------------------------------------------------------------

async def _three_pages(ctx, session_size=20, limit=10):
    node = _make_wrapper(session_size=session_size)
    r1 = await run_executor.run(node, ctx, limit=limit, cursor={})
    r2 = await run_executor.run(node, ctx, limit=limit, cursor=r1.next_page)
    r3 = await run_executor.run(node, ctx, limit=limit, cursor=r2.next_page)
    return r1, r2, r3


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pages_1_and_2_served_from_cache(ctx):
    """Pages 1 and 2 are served from the same generation (warm hits)."""
    node = _make_wrapper(session_size=20)
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    gen1 = r1.next_page["w"]["gen"]

    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    gen2 = r2.next_page["w"]["gen"]

    # Both pages share the same generation id
    assert gen1 == gen2


@pytest.mark.asyncio
async def test_page3_triggers_rebuild_with_new_generation(ctx):
    """Page 3 exhausts the 20-item cache, triggering a cold rebuild with a new gen."""
    r1, r2, r3 = await _three_pages(ctx)

    gen_before = r2.next_page["w"]["gen"]
    gen_after = r3.next_page["w"]["gen"]

    assert gen_before != gen_after, "Page 3 rebuild must produce a new generation id"


@pytest.mark.asyncio
async def test_page3_items_continue_from_page2_no_gap(ctx):
    """Items on page 3 immediately follow items on page 2 (no gap, no overlap)."""
    r1, r2, r3 = await _three_pages(ctx)

    ids_p2 = [item["id"] for item in r2.data]
    ids_p3 = [item["id"] for item in r3.data]

    last_p2 = max(ids_p2)
    first_p3 = min(ids_p3)

    # No gap: the first item of page 3 immediately follows the last item of page 2
    assert first_p3 == last_p2 + 1, (
        f"Expected page 3 to start at {last_p2 + 1}, got {first_p3}"
    )


@pytest.mark.asyncio
async def test_page3_items_no_overlap_with_page1_and_page2(ctx):
    """Items on page 3 must not repeat any item from pages 1 or 2."""
    r1, r2, r3 = await _three_pages(ctx)

    seen_ids = {item["id"] for item in r1.data} | {item["id"] for item in r2.data}
    p3_ids = {item["id"] for item in r3.data}

    overlap = seen_ids & p3_ids
    assert not overlap, f"Page 3 overlaps with earlier pages: {overlap}"


@pytest.mark.asyncio
async def test_page3_has_correct_item_count(ctx):
    """Page 3 (post-rebuild) still returns exactly `limit` items."""
    _, _, r3 = await _three_pages(ctx)
    assert len(r3.data) == 10


@pytest.mark.asyncio
async def test_all_ids_across_three_pages_are_unique(ctx):
    """All item IDs across pages 1, 2 and 3 are distinct."""
    r1, r2, r3 = await _three_pages(ctx)

    all_ids = (
        [item["id"] for item in r1.data]
        + [item["id"] for item in r2.data]
        + [item["id"] for item in r3.data]
    )
    assert len(all_ids) == len(set(all_ids)), "Duplicate IDs found across pages"


@pytest.mark.asyncio
async def test_all_ids_are_globally_sequential(ctx):
    """Items across pages 1-3 form a gapless sequence starting from 0."""
    r1, r2, r3 = await _three_pages(ctx)

    all_ids = sorted(
        item["id"]
        for page in (r1.data, r2.data, r3.data)
        for item in page
    )
    expected = list(range(30))
    assert all_ids == expected, f"Expected {expected}, got {all_ids}"


@pytest.mark.asyncio
async def test_continuation_works_for_smaller_session_size(ctx):
    """Continuation rebuild works correctly with session_size=6 and limit=3."""
    node = _make_wrapper(session_size=6)

    # Pages 1 and 2 from cache
    r1 = await run_executor.run(node, ctx, limit=3, cursor={})
    r2 = await run_executor.run(node, ctx, limit=3, cursor=r1.next_page)
    # Page 3 triggers rebuild
    r3 = await run_executor.run(node, ctx, limit=3, cursor=r2.next_page)

    # New generation
    assert r3.next_page["w"]["gen"] != r2.next_page["w"]["gen"]

    # No overlap
    seen = {item["id"] for item in r1.data} | {item["id"] for item in r2.data}
    new = {item["id"] for item in r3.data}
    assert not (seen & new)

    # No gap
    last_p2 = max(item["id"] for item in r2.data)
    first_p3 = min(item["id"] for item in r3.data)
    assert first_p3 == last_p2 + 1
