import pytest
import fakeredis.aioredis

from smartfeed.execution.redis_lock import RedisLock


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.mark.asyncio
async def test_lock_acquired(redis):
    async with RedisLock(redis, "test:lock") as acquired:
        assert acquired is True
        assert await redis.exists("test:lock")
    assert not await redis.exists("test:lock")


@pytest.mark.asyncio
async def test_lock_contention(redis):
    async with RedisLock(redis, "test:lock") as first:
        assert first is True
        async with RedisLock(redis, "test:lock") as second:
            assert second is False


@pytest.mark.asyncio
async def test_lock_released_after_first(redis):
    async with RedisLock(redis, "test:lock"):
        pass
    async with RedisLock(redis, "test:lock") as acquired:
        assert acquired is True
