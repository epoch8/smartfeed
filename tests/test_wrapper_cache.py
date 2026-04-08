import pytest
import fakeredis.aioredis

from smartfeed.models.wrapper import Wrapper, WrapperCache
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from tests.conftest import METHODS


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=METHODS, redis=redis)


def _make_wrapper(node_id="cached", session_size=50, session_ttl=300):
    return Wrapper(
        node_id=node_id,
        cache=WrapperCache(session_size=session_size, session_ttl=session_ttl),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )


@pytest.mark.asyncio
async def test_cache_cold_build(ctx, redis):
    node = _make_wrapper()
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(result.data) == 10
    # Some Redis key should exist
    all_keys = list(await redis.keys("sf:*"))
    assert len(all_keys) >= 1


@pytest.mark.asyncio
async def test_cache_warm_hit(ctx):
    node = _make_wrapper()
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    # Different page
    assert r2.data[0]["id"] != r1.data[0]["id"]
    assert len(r2.data) == 10


@pytest.mark.asyncio
async def test_generation_id_in_cursor(ctx):
    node = _make_wrapper()
    result = await run_executor.run(node, ctx, limit=10, cursor={})
    assert "gen" in result.next_page.get("cached", {})


@pytest.mark.asyncio
async def test_stale_gen_resets_to_page1(ctx, redis):
    node = _make_wrapper()
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    # Flush to simulate TTL expiry
    await redis.flushall()
    stale_cursor = {"cached": {"page": 5, "gen": "stale_nonce"}}
    r2 = await run_executor.run(node, ctx, limit=10, cursor=stale_cursor)
    # Should reset: page 2 in next cursor (just served page 1)
    assert r2.next_page["cached"]["page"] == 2
    assert r2.next_page["cached"]["gen"] != "stale_nonce"


@pytest.mark.asyncio
async def test_wrapper_without_cache_passthrough(ctx):
    """Wrapper without cache passes through to child directly."""
    node = Wrapper(
        node_id="passthrough",
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    result = await run_executor.run(node, ctx, limit=5, cursor={})
    assert len(result.data) == 5
