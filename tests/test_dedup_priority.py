import pytest
import fakeredis.aioredis

from smartfeed.models import FeedConfig
from smartfeed.models.base import FeedResult
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


async def promo_source(user_id, limit, next_page, **kw):
    data = [{"id": i, "val": "promo"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def regular_source(user_id, limit, next_page, **kw):
    data = [{"id": i, "val": "regular"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


PRIO_METHODS = {"promo": promo_source, "regular": regular_source}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=PRIO_METHODS, redis=redis)


@pytest.mark.asyncio
async def test_wrapper_dedup_priority_override(ctx):
    """Inner wrapper with dedup_priority=3 overrides child subfeed priorities."""
    config = FeedConfig.model_validate(
        {
            "version": "2",
            "feed": {
                "type": "wrapper",
                "node_id": "outer",
                "dedup": {"dedup_key": "id"},
                "data": {
                    "type": "merger_append",
                    "node_id": "top",
                    "items": [
                        {"type": "subfeed", "subfeed_id": "promo", "method_name": "promo", "dedup_priority": 10},
                        {
                            "type": "wrapper",
                            "node_id": "inner",
                            "dedup_priority": 3,
                            "data": {"type": "subfeed", "subfeed_id": "regular", "method_name": "regular"},
                        },
                    ],
                },
            },
        }
    )

    result = await run_executor.run(config.feed, ctx, limit=10, cursor={})

    # promo(10) > inner wrapper override(3) -> all promo
    for item in result.data:
        assert item["val"] == "promo"
