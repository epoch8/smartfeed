import pytest
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from smartfeed.models.mixers import MergerPercentage, MergerPercentageItem, MergerAppend, MergerPositional
from tests.conftest import METHODS


@pytest.fixture
def ctx():
    return ExecutionContext(session_id="s1", methods_dict=METHODS, redis=None)


@pytest.mark.asyncio
async def test_executor_runs_subfeed(ctx):
    node = SubFeed(subfeed_id="items", method_name="items")
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    assert len(result.data) == 5
    assert result.data[0]["_smartfeed_debug_info"]["source"] == "items"


@pytest.mark.asyncio
async def test_percentage_40_60(ctx):
    node = MergerPercentage(
        node_id="mix",
        items=[
            MergerPercentageItem(
                percentage=40,
                data=SubFeed(subfeed_id="a", method_name="items"),
            ),
            MergerPercentageItem(
                percentage=60,
                data=SubFeed(subfeed_id="b", method_name="items"),
            ),
        ],
    )
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(result.data) == 10
    sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
    assert sources.count("a") == 4
    assert sources.count("b") == 6


@pytest.mark.asyncio
async def test_append_two_subfeeds(ctx):
    node = MergerAppend(
        node_id="append",
        items=[
            SubFeed(subfeed_id="a", method_name="items"),
            SubFeed(subfeed_id="b", method_name="items"),
        ],
    )
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(result.data) == 10


@pytest.mark.asyncio
async def test_positional_inserts_at_positions(ctx):
    node = MergerPositional(
        node_id="pos",
        positions=[1, 3],
        positional=SubFeed(subfeed_id="promo", method_name="items"),
        default=SubFeed(subfeed_id="regular", method_name="items"),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
    assert sources[0] == "promo"  # position 1
    assert sources[1] == "regular"
    assert sources[2] == "promo"  # position 3
