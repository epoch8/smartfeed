"""Regression: RedisLock assumes bytes; crashes under decode_responses=True.

redis_lock.py:35 does ``val.decode()`` in __aexit__. With a Redis client
configured ``decode_responses=True`` (a very common setup), the reply is already
str and release raises AttributeError, breaking the shared-cache cold build. The
rest of the codebase (seen-set load, wrapper.py:269) already handles both
bytes/str, so this is an internal inconsistency.

Also included: a green characterization test showing the token check itself is
correct (a lock whose value was overwritten by another owner is NOT deleted on
release). The residual issue there is the non-atomic get-then-delete (TOCTOU),
which cannot be reproduced deterministically without concurrency injection and is
documented separately rather than asserted here.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution.redis_lock import RedisLock
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_lock_release_with_decode_responses():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    async with RedisLock(redis, "k", ttl=5) as acquired:
        assert acquired is True
    # Release must have deleted the key without raising.
    assert not await redis.exists("k")


@pytest.mark.asyncio
async def test_shared_cold_build_with_decode_responses():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    src = S.ScriptedSource(S.unique_pool(100))
    node = S.wrapper(S.subfeed("src", "src"), node_id="w", session_size=20, cache_key="shared")
    ctx = S.make_ctx({"src": src}, redis=redis)
    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(r.data) == 10


@pytest.mark.asyncio
async def test_lock_bytes_mode_is_fine_control():
    redis = fakeredis.aioredis.FakeRedis()  # bytes mode (default)
    async with RedisLock(redis, "k", ttl=5) as acquired:
        assert acquired is True
    assert not await redis.exists("k")


@pytest.mark.asyncio
async def test_lock_does_not_delete_foreign_token():
    """Characterization: if another owner overwrote the key, release leaves it.
    (The token compare-and-delete is correct in the non-racy case.)"""
    redis = fakeredis.aioredis.FakeRedis()
    lock = RedisLock(redis, "k", ttl=30)
    acquired = await lock.__aenter__()
    assert acquired is True
    # Simulate expiry + re-acquire by a different owner.
    await redis.set("k", b"someone-elses-token")
    await lock.__aexit__(None, None, None)
    # Foreign token must survive -- we must not delete a lock we no longer own.
    assert await redis.get("k") == b"someone-elses-token"
