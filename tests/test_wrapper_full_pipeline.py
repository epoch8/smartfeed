import pytest
import fakeredis.aioredis

from smartfeed.models import FeedConfig
from smartfeed.models.base import FeedResult
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


async def source_a(user_id, limit, next_page, **kw):
    data = [{"id": i, "val": f"a_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def source_b(user_id, limit, next_page, **kw):
    data = [{"id": i + limit // 2, "val": f"b_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def reverse_rerank(items, session_id):
    return list(reversed(items))


PIPELINE_METHODS = {"source_a": source_a, "source_b": source_b, "reverse": reverse_rerank}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="test_full", methods_dict=PIPELINE_METHODS, redis=redis)


@pytest.mark.asyncio
async def test_full_pipeline(ctx):
    config = FeedConfig.parse_obj({
        "version": "2",
        "feed": {
            "type": "wrapper", "node_id": "full",
            "cache": {"session_size": 30, "session_ttl": 300},
            "rerank": {"method_name": "reverse"},
            "dedup": {"dedup_key": "id", "overfetch_factor": 2},
            "data": {
                "type": "merger_percentage", "node_id": "mix",
                "items": [
                    {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "a", "method_name": "source_a", "dedup_priority": 5}},
                    {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "b", "method_name": "source_b", "dedup_priority": 1}},
                ],
            },
        },
    })

    r1 = await run_executor.run(config.feed, ctx, limit=10, cursor={})

    # No duplicate ids
    ids = [item["id"] for item in r1.data]
    assert len(ids) == len(set(ids))

    # Has generation_id
    assert "gen" in r1.next_page.get("full", {})

    # Has debug info
    for item in r1.data:
        info = item["_smartfeed_debug_info"]
        assert "source" in info

    # Page 2 from cache
    r2 = await run_executor.run(config.feed, ctx, limit=10, cursor=r1.next_page)
    assert len(r2.data) > 0
