import pytest
import fakeredis.aioredis

from smartfeed.models.wrapper import Wrapper, WrapperDedup
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.mixers import MergerAppend
from smartfeed.models.base import FeedResult
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


async def make_dupes_a(user_id, limit, next_page, **kw):
    data = [{"id": i, "val": f"a_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def make_dupes_b(user_id, limit, next_page, **kw):
    data = [{"id": i, "val": f"b_{i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


DEDUP_METHODS = {"dupes_a": make_dupes_a, "dupes_b": make_dupes_b}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=DEDUP_METHODS, redis=redis)


@pytest.mark.asyncio
async def test_dedup_removes_duplicates(ctx):
    node = Wrapper(
        node_id="deduped",
        dedup=WrapperDedup(dedup_key="id", overfetch_factor=2),
        data=MergerAppend(
            node_id="mix",
            items=[
                SubFeed(subfeed_id="a", method_name="dupes_a"),
                SubFeed(subfeed_id="b", method_name="dupes_b"),
            ],
        ),
    )
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    ids = [item["id"] for item in result.data]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_dedup_priority_higher_wins(ctx):
    node = Wrapper(
        node_id="deduped",
        dedup=WrapperDedup(dedup_key="id"),
        data=MergerAppend(
            node_id="mix",
            items=[
                SubFeed(subfeed_id="a", method_name="dupes_a", dedup_priority=5),
                SubFeed(subfeed_id="b", method_name="dupes_b", dedup_priority=1),
            ],
        ),
    )
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    for item in result.data:
        assert item["val"].startswith("a_")


@pytest.mark.asyncio
async def test_dedup_equal_priority_first_seen_wins(ctx):
    node = Wrapper(
        node_id="deduped",
        dedup=WrapperDedup(dedup_key="id"),
        data=MergerAppend(
            node_id="mix",
            items=[
                SubFeed(subfeed_id="a", method_name="dupes_a", dedup_priority=1),
                SubFeed(subfeed_id="b", method_name="dupes_b", dedup_priority=1),
            ],
        ),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    for item in result.data:
        assert item["val"].startswith("a_")
