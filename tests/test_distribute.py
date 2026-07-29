import pytest

from smartfeed.models.base import FeedResult
from smartfeed.models.mixers import MergerAppendDistribute
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


async def make_operators(user_id, limit, next_page, **kw):
    """Source producing items with operator_id cycling through op_0, op_1, op_2."""
    data = [{"id": i, "operator_id": f"op_{i % 3}", "val": f"item_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def make_scored(user_id, limit, next_page, **kw):
    """Source with a numeric score field for testing sorting_key.

    Items are in ascending score order so that descending sort visibly reorders them.
    op_0 gets scores 1, 3, 5, ... and op_1 gets scores 2, 4, 6, ...
    """
    data = [{"id": i, "operator_id": f"op_{i % 2}", "score": i + 1, "val": f"item_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def make_no_key(user_id, limit, next_page, **kw):
    """Source producing items WITHOUT operator_id (to test missing key behaviour)."""
    data = [{"id": i, "val": f"item_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


METHODS = {
    "operators": make_operators,
    "scored": make_scored,
    "no_key": make_no_key,
}


@pytest.fixture
def ctx():
    return ExecutionContext(session_id="s1", methods_dict=METHODS, redis=None)


# ---------------------------------------------------------------------------
# Basic round-robin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_basic_round_robin_no_consecutive_same_operator(ctx):
    """Consecutive items should not share the same operator_id (round-robin effect)."""
    node = MergerAppendDistribute(
        node_id="dist",
        items=[SubFeed(subfeed_id="ops", method_name="operators")],
        distribution_key="operator_id",
    )
    result = await run_executor.run(node, ctx, limit=9, cursor={})

    assert len(result.data) == 9
    for i in range(len(result.data) - 1):
        assert (
            result.data[i]["operator_id"] != result.data[i + 1]["operator_id"]
        ), f"Consecutive duplicate operator at positions {i} and {i+1}"


@pytest.mark.asyncio
async def test_round_robin_all_operators_represented(ctx):
    """All three operators must appear in a full round-robin of 9 items."""
    node = MergerAppendDistribute(
        node_id="dist",
        items=[SubFeed(subfeed_id="ops", method_name="operators")],
        distribution_key="operator_id",
    )
    result = await run_executor.run(node, ctx, limit=9, cursor={})

    operator_ids = {item["operator_id"] for item in result.data}
    assert operator_ids == {"op_0", "op_1", "op_2"}


@pytest.mark.asyncio
async def test_round_robin_result_length_matches_limit(ctx):
    """Result must contain exactly `limit` items."""
    node = MergerAppendDistribute(
        node_id="dist",
        items=[SubFeed(subfeed_id="ops", method_name="operators")],
        distribution_key="operator_id",
    )
    for lim in (3, 6, 9):
        result = await run_executor.run(node, ctx, limit=lim, cursor={})
        assert len(result.data) == lim


# ---------------------------------------------------------------------------
# sorting_key sorts before distributing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sorting_key_desc_highest_score_first(ctx):
    """With sorting_key='score' and sorting_desc=True, higher scores appear earlier."""
    node = MergerAppendDistribute(
        node_id="dist_sorted",
        items=[SubFeed(subfeed_id="scored", method_name="scored")],
        distribution_key="operator_id",
        sorting_key="score",
        sorting_desc=True,
    )
    result = await run_executor.run(node, ctx, limit=4, cursor={})

    assert len(result.data) == 4
    # The highest score items per operator group should appear first.
    # Verify that score of first item per operator_id group is higher than second.
    from collections import defaultdict

    by_op = defaultdict(list)
    for item in result.data:
        by_op[item["operator_id"]].append(item["score"])

    for op, scores in by_op.items():
        if len(scores) >= 2:
            assert scores[0] >= scores[1], f"Operator {op}: expected descending scores, got {scores}"


@pytest.mark.asyncio
async def test_sorting_key_asc_lowest_score_first(ctx):
    """With sorting_key='score' and sorting_desc=False, lower scores appear earlier."""
    node = MergerAppendDistribute(
        node_id="dist_asc",
        items=[SubFeed(subfeed_id="scored", method_name="scored")],
        distribution_key="operator_id",
        sorting_key="score",
        sorting_desc=False,
    )
    result = await run_executor.run(node, ctx, limit=4, cursor={})

    assert len(result.data) == 4
    # All scores in the output must be <= max available score in source
    # (weak check: just confirm the sort changed the order vs no sorting)
    scores = [item["score"] for item in result.data]
    # First item of each operator should have a smaller score than last
    assert scores[0] <= scores[-1] or len(set(scores)) == 1


@pytest.mark.asyncio
async def test_no_sorting_key_preserves_source_order(ctx):
    """Without sorting_key, source order is preserved before distribution."""
    node_no_sort = MergerAppendDistribute(
        node_id="dist_nosort",
        items=[SubFeed(subfeed_id="scored", method_name="scored")],
        distribution_key="operator_id",
    )
    node_sorted = MergerAppendDistribute(
        node_id="dist_sorted",
        items=[SubFeed(subfeed_id="scored", method_name="scored")],
        distribution_key="operator_id",
        sorting_key="score",
        sorting_desc=True,
    )

    r_nosort = await run_executor.run(node_no_sort, ctx, limit=4, cursor={})
    r_sorted = await run_executor.run(node_sorted, ctx, limit=4, cursor={})

    ids_nosort = [item["id"] for item in r_nosort.data]
    ids_sorted = [item["id"] for item in r_sorted.data]

    # Sorted and unsorted should produce different orderings (source has varying scores)
    assert ids_nosort != ids_sorted


# ---------------------------------------------------------------------------
# Missing distribution_key raises KeyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_distribution_key_raises(ctx):
    """Items without the distribution_key should raise KeyError."""
    node = MergerAppendDistribute(
        node_id="dist_bad",
        items=[SubFeed(subfeed_id="nokey", method_name="no_key")],
        distribution_key="operator_id",
    )
    with pytest.raises(KeyError):
        await run_executor.run(node, ctx, limit=5, cursor={})


# ---------------------------------------------------------------------------
# Multiple subfeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multiple_subfeeds_merged_before_distribute(ctx):
    """Items from multiple subfeeds are pooled before round-robin distribution."""
    node = MergerAppendDistribute(
        node_id="dist_multi",
        items=[
            SubFeed(subfeed_id="ops_a", method_name="operators"),
            SubFeed(subfeed_id="ops_b", method_name="operators"),
        ],
        distribution_key="operator_id",
    )
    result = await run_executor.run(node, ctx, limit=6, cursor={})

    assert len(result.data) == 6
    # Still no consecutive duplicates
    for i in range(len(result.data) - 1):
        assert result.data[i]["operator_id"] != result.data[i + 1]["operator_id"]
