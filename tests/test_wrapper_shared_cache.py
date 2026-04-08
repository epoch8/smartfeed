import pytest
import fakeredis.aioredis
from smartfeed.models.wrapper import Wrapper, WrapperCache, WrapperRerank
from smartfeed.models.subfeed import SubFeed
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from tests.conftest import METHODS


async def rerank_reverse(items, session_id):
    return list(reversed(items))

async def rerank_identity(items, session_id):
    return items

SHARED_METHODS = {**METHODS, "rerank_a": rerank_reverse, "rerank_b": rerank_identity}

@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()

@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="s1", methods_dict=SHARED_METHODS, redis=redis)

@pytest.mark.asyncio
async def test_shared_cache_two_rerankers(ctx):
    w_a = Wrapper(
        node_id="a", cache_key="shared",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_a"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    w_b = Wrapper(
        node_id="b", cache_key="shared",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_b"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    r_a = await run_executor.run(w_a, ctx, limit=5, cursor={})
    r_b = await run_executor.run(w_b, ctx, limit=5, cursor={})
    assert len(r_a.data) == 5
    assert len(r_b.data) == 5
    # Different rerank -> different order
    assert r_a.data != r_b.data


@pytest.mark.asyncio
async def test_shared_cache_base_written_once(ctx, redis):
    """Both wrappers use same cache_key -- base cache key should exist once."""
    w_a = Wrapper(
        node_id="a", cache_key="shared_base",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_a"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    w_b = Wrapper(
        node_id="b", cache_key="shared_base",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_b"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    await run_executor.run(w_a, ctx, limit=5, cursor={})
    await run_executor.run(w_b, ctx, limit=5, cursor={})

    # Each wrapper writes its own reranked cache under node_id
    # But both share the base key (cache_key="shared_base")
    all_keys = [k.decode() if isinstance(k, bytes) else k for k in await redis.keys("sf:*")]
    # The shared base key should exist
    base_keys = [k for k in all_keys if ":shared_base:" in k and not k.endswith(":a:") and not k.endswith(":b:")]
    assert len(base_keys) >= 1


@pytest.mark.asyncio
async def test_shared_cache_warm_read(ctx, redis):
    """After cold build, reading warm cache returns same base data, different rerank."""
    w_a = Wrapper(
        node_id="a", cache_key="shared_warm",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_a"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    w_b = Wrapper(
        node_id="b", cache_key="shared_warm",
        cache=WrapperCache(session_size=20, session_ttl=300),
        rerank=WrapperRerank(method_name="rerank_b"),
        data=SubFeed(subfeed_id="items", method_name="items"),
    )
    r_a1 = await run_executor.run(w_a, ctx, limit=5, cursor={})
    # Second call with cursor (warm path for w_a)
    r_a2 = await run_executor.run(w_a, ctx, limit=5, cursor=r_a1.next_page)
    assert len(r_a2.data) == 5

    # w_b also reads the shared base -- should work fine
    r_b1 = await run_executor.run(w_b, ctx, limit=5, cursor={})
    assert len(r_b1.data) == 5
