import pytest

from smartfeed.models.base import FeedResult
from smartfeed.models.mixers import MergerPercentageGradient, MergerPercentageItem
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


async def make_source(prefix, user_id, limit, next_page, **kw):
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": f"{prefix}_{start + i}", "val": prefix} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


METHODS = {
    "source_a": lambda *a, **kw: make_source("a", *a, **kw),
    "source_b": lambda *a, **kw: make_source("b", *a, **kw),
}


@pytest.fixture
def ctx():
    return ExecutionContext(session_id="s1", methods_dict=METHODS, redis=None)


def _make_gradient(pct_from=80, pct_to=20, step=10, size_to_step=10):
    return MergerPercentageGradient(
        node_id="grad",
        item_from=MergerPercentageItem(
            percentage=pct_from,
            data=SubFeed(subfeed_id="source_a", method_name="source_a"),
        ),
        item_to=MergerPercentageItem(
            percentage=pct_to,
            data=SubFeed(subfeed_id="source_b", method_name="source_b"),
        ),
        step=step,
        size_to_step=size_to_step,
    )


@pytest.mark.asyncio
async def test_basic_80_20_page1(ctx):
    """On page 1 with 80/20 split and limit=10: 8 from source_a, 2 from source_b."""
    node = _make_gradient(pct_from=80, pct_to=20, step=10, size_to_step=10)
    result = await run_executor.run(node, ctx, limit=10, cursor={})

    assert len(result.data) == 10
    sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
    assert sources.count("source_a") == 8
    assert sources.count("source_b") == 2


@pytest.mark.asyncio
async def test_both_sources_present(ctx):
    """Items from both sources appear in the result."""
    node = _make_gradient(pct_from=80, pct_to=20, step=10, size_to_step=10)
    result = await run_executor.run(node, ctx, limit=10, cursor={})

    sources = {item["_smartfeed_debug_info"]["source"] for item in result.data}
    assert "source_a" in sources
    assert "source_b" in sources


@pytest.mark.asyncio
async def test_ratio_shifts_on_later_page(ctx):
    """Later pages have a higher proportion of source_b than page 1 (gradient effect)."""
    node = _make_gradient(pct_from=80, pct_to=20, step=10, size_to_step=10)

    # Page 1
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    sources_p1 = [item["_smartfeed_debug_info"]["source"] for item in r1.data]
    a_count_p1 = sources_p1.count("source_a")
    b_count_p1 = sources_p1.count("source_b")

    # Page 2 (cursor carries page state for the gradient node)
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    sources_p2 = [item["_smartfeed_debug_info"]["source"] for item in r2.data]
    a_count_p2 = sources_p2.count("source_a")
    b_count_p2 = sources_p2.count("source_b")

    # The gradient should shift: fewer source_a and more source_b on page 2
    assert a_count_p2 < a_count_p1
    assert b_count_p2 > b_count_p1


@pytest.mark.asyncio
async def test_step_parameter_controls_shift(ctx):
    """A larger step causes a bigger ratio change between pages."""
    node_small_step = _make_gradient(pct_from=80, pct_to=20, step=5, size_to_step=10)
    node_large_step = _make_gradient(pct_from=80, pct_to=20, step=20, size_to_step=10)

    # Compute ratio difference between page 1 and page 2 for each node
    def _b_count(node, cursor):
        import asyncio
        r = asyncio.get_event_loop().run_until_complete(
            run_executor.run(node, ctx, limit=10, cursor=cursor)
        )
        sources = [item["_smartfeed_debug_info"]["source"] for item in r.data]
        return sources.count("source_b"), r.next_page

    # Use coroutine approach instead
    r1_small = await run_executor.run(node_small_step, ctx, limit=10, cursor={})
    r2_small = await run_executor.run(node_small_step, ctx, limit=10, cursor=r1_small.next_page)

    r1_large = await run_executor.run(node_large_step, ctx, limit=10, cursor={})
    r2_large = await run_executor.run(node_large_step, ctx, limit=10, cursor=r1_large.next_page)

    def b_count(result):
        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        return sources.count("source_b")

    shift_small = b_count(r2_small) - b_count(r1_small)
    shift_large = b_count(r2_large) - b_count(r1_large)

    # Larger step => bigger shift in ratio
    assert shift_large >= shift_small


@pytest.mark.asyncio
async def test_result_length_matches_limit(ctx):
    """Result always contains exactly `limit` items."""
    node = _make_gradient()
    for lim in (5, 10, 15):
        result = await run_executor.run(node, ctx, limit=lim, cursor={})
        assert len(result.data) == lim


@pytest.mark.asyncio
async def test_cursor_advances_page(ctx):
    """next_page cursor increments the internal page counter for the gradient node."""
    node = _make_gradient()
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    # The gradient stores its own page in cursor under its node_id
    assert r1.next_page.get("grad", {}).get("page") == 2

    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    assert r2.next_page.get("grad", {}).get("page") == 3


@pytest.mark.asyncio
async def test_gradient_eventually_reaches_100_pct_to(ctx):
    """After enough pages the ratio fully shifts to source_b (100%)."""
    # step=50 will flip to 100% after 2 pages
    node = _make_gradient(pct_from=80, pct_to=20, step=50, size_to_step=10)

    cursor = {}
    for _ in range(5):
        r = await run_executor.run(node, ctx, limit=10, cursor=cursor)
        cursor = r.next_page

    sources = [item["_smartfeed_debug_info"]["source"] for item in r.data]
    # At full shift, all items come from source_b
    assert sources.count("source_b") == 10
    assert sources.count("source_a") == 0
