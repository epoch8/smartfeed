import pytest
import fakeredis.aioredis

from smartfeed.models.wrapper import Wrapper, WrapperCache, WrapperRerank
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from tests.conftest import METHODS


async def reverse_rerank(items, session_id):
    return list(reversed(items))


async def bad_rerank(items, session_id):
    return items[:1]  # contract violation


RERANK_METHODS = {**METHODS, "reverse": reverse_rerank, "bad": bad_rerank}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=RERANK_METHODS, redis=redis)


@pytest.mark.asyncio
async def test_rerank_with_cache(ctx):
    node = Wrapper(
        node_id="reranked",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="reverse"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    assert len(result.data) == 5
    # Rerank reversed the order: first item should have highest original id
    id_first = result.data[0]["id"]
    id_last = result.data[-1]["id"]
    assert id_first > id_last  # reversed = descending ids


@pytest.mark.asyncio
async def test_rerank_without_cache(ctx):
    node = Wrapper(
        node_id="reranked",
        rerank=WrapperRerank(method_name="reverse"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    assert len(result.data) == 5
    # The rerank must actually run: "reverse" flips the order, so a silently
    # skipped rerank (ascending ids) fails here.
    assert result.data[0]["id"] > result.data[-1]["id"]


@pytest.mark.asyncio
async def test_rerank_contract_violation(ctx):
    node = Wrapper(
        node_id="bad",
        cache=WrapperCache(session_size=10, session_ttl=300),
        rerank=WrapperRerank(method_name="bad"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    with pytest.raises(ValueError, match="must return exactly"):
        await run_executor.run(node, ctx, limit=5, cursor={})


@pytest.mark.asyncio
async def test_rerank_stamps_rerank_position(ctx):
    node = Wrapper(
        node_id="reranked",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="reverse"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    for item in result.data:
        info = item["_smartfeed_debug_info"]["reranked"]
        assert "rerank_position" in info
        assert "smartfeed_position" in info
