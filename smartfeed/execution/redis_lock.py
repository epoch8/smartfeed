from __future__ import annotations

import secrets
from typing import Any

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import WatchError


class RedisLock:
    """Distributed lock via SETNX with unique token.

    async with RedisLock(redis, "key") as acquired:
        if acquired:
            ...  # we hold the lock
        else:
            ...  # someone else holds it
    """

    def __init__(self, redis: "AsyncRedis[Any]", key: str, ttl: int = 10):
        self._redis = redis
        self._key = key
        self._ttl = ttl
        self._token = secrets.token_hex(8)
        self._owned = False

    async def __aenter__(self) -> bool:
        self._owned = bool(await self._redis.set(self._key, self._token, nx=True, ex=self._ttl))
        return self._owned

    async def __aexit__(self, *exc: object) -> None:
        if not self._owned:
            return
        # Atomic compare-and-delete: WATCH the key so that if it changes between our
        # read and the DELETE (our TTL expired and someone else acquired), the EXEC
        # aborts instead of deleting the new holder's lock.
        async with self._redis.pipeline(transaction=True) as pipe:
            try:
                await pipe.watch(self._key)
                val = await self._redis.get(self._key)
                if val is None:
                    return  # lock expired; nothing to release
                # Redis clients configured with decode_responses=True return str, not bytes.
                token = val.decode() if isinstance(val, bytes) else val
                if token != self._token:
                    return  # lock expired and re-acquired by someone else; not ours
                pipe.multi()
                pipe.delete(self._key)
                await pipe.execute()
            except WatchError:
                pass  # key changed under us -> it is no longer ours to delete
