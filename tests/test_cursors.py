import pytest
import fakeredis.aioredis

from smartfeed.models import FeedConfig
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from tests.conftest import METHODS


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=METHODS, redis=redis)


@pytest.mark.asyncio
async def test_cached_wrapper_hides_child_cursors(ctx):
    config = FeedConfig.model_validate(
        {
            "version": "2",
            "feed": {
                "type": "wrapper",
                "node_id": "cached",
                "cache": {"session_size": 50, "session_ttl": 300},
                "data": {"type": "subfeed", "subfeed_id": "items", "method_name": "items"},
            },
        }
    )

    result = await run_executor.run(config.feed, ctx, limit=10, cursor={})

    # Only wrapper cursor, NOT subfeed cursor
    assert "cached" in result.next_page
    assert "items" not in result.next_page
    assert "gen" in result.next_page["cached"]


@pytest.mark.asyncio
async def test_uncached_wrapper_exposes_child_cursors(ctx):
    config = FeedConfig.model_validate(
        {
            "version": "2",
            "feed": {
                "type": "wrapper",
                "node_id": "passthrough",
                "data": {"type": "subfeed", "subfeed_id": "items", "method_name": "items"},
            },
        }
    )

    result = await run_executor.run(config.feed, ctx, limit=10, cursor={})

    # Child cursor is visible (no cache to hide it)
    assert "items" in result.next_page
