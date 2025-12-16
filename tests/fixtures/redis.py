import pytest
import pytest_asyncio
import redis
from redis.asyncio import Redis as AsyncRedis


@pytest_asyncio.fixture(scope="function")
async def redis_client(request):
    """Provide a Redis client for tests.

    If Redis is not available on localhost:6379, skip tests that depend on it.
    """

    if request.param == "async":
        client = AsyncRedis(host="localhost", port=6379)
        try:
            await client.ping()
        except Exception:  # pragma: no cover
            pytest.skip("Redis is not available on localhost:6379")
        yield client
        await client.aclose()
        return

    client = redis.Redis(host="localhost", port=6379, db=0)
    try:
        client.ping()
    except Exception:  # pragma: no cover
        pytest.skip("Redis is not available on localhost:6379")
    yield client
    client.close()
