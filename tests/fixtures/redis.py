import pytest
import redis
from redis.asyncio import Redis as AsyncRedis


@pytest.fixture(scope="function")
def redis_client(request):
    if request.param == "async":
        return AsyncRedis(host="localhost", port=6379)
    return redis.Redis(host="localhost", port=6379, db=0)
