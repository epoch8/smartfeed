from __future__ import annotations

import secrets

from redis.asyncio import Redis as AsyncRedis


class RedisLock:
    """Distributed lock via SETNX with unique token.

    async with RedisLock(redis, "key") as acquired:
        if acquired:
            ...  # we hold the lock
        else:
            ...  # someone else holds it
    """

    def __init__(self, redis: AsyncRedis, key: str, ttl: int = 10):
        self._redis = redis
        self._key = key
        self._ttl = ttl
        self._token = secrets.token_hex(8)
        self._owned = False

    async def __aenter__(self) -> bool:
        self._owned = bool(
            await self._redis.set(self._key, self._token, nx=True, ex=self._ttl)
        )
        return self._owned

    async def __aexit__(self, *exc):
        if not self._owned:
            return
        val = await self._redis.get(self._key)
        if val is not None:
            # Redis clients configured with decode_responses=True return str, not bytes.
            token = val.decode() if isinstance(val, bytes) else val
            if token == self._token:
                await self._redis.delete(self._key)
