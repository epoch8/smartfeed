"""Documented silent downgrades: these configurations must
degrade gracefully, not crash -- and must not write state they cannot honor.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_cache_without_redis_degrades_to_passthrough():
    node = S.wrapper(S.subfeed("s", "src"), session_size=20)
    ctx = S.make_ctx({"src": S.ScriptedSource(S.unique_pool(50))}, redis=None)
    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    assert [it["id"] for it in r1.data] == list(range(10))
    # Passthrough exposes the raw child cursor and keeps paginating without Redis.
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    assert [it["id"] for it in r2.data] == list(range(10, 20))


@pytest.mark.asyncio
async def test_cache_key_without_cache_is_pure_passthrough():
    redis = fakeredis.aioredis.FakeRedis()
    node = S.wrapper(S.subfeed("s", "src"), cache_key="shared")  # no cache block
    ctx = S.make_ctx({"src": S.ScriptedSource(S.unique_pool(50))}, redis=redis)
    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(r.data) == 10
    assert await redis.keys("*") == []  # no cache/shared/lock keys written
