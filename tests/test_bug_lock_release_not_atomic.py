"""Regression: RedisLock release is GET-then-DELETE, not atomic.

If the lock expires after the release-path GET returned the holder's own token
but before the DELETE executes, and another coroutine acquires the lock in that
window, the DELETE removes the NEW holder's lock: mutual exclusion is silently
lost for a third contender.

Correct behavior: release must be compare-and-delete in one atomic step (Lua),
so a holder can only ever delete its own token. This test forces the expiry +
re-acquire interleaving deterministically via a hook between GET and DELETE.

(The related no-TTL-extension concern -- a build outliving the 30s
lock TTL -- is a design decision on the fix and is not encoded here.)
"""

from typing import Awaitable, Callable, Optional

import pytest
import fakeredis.aioredis

from smartfeed.execution.redis_lock import RedisLock

GetHook = Callable[[str, Optional[bytes]], Awaitable[None]]


class HookedRedis:
    """Proxy that awaits a hook after each GET returns (to interleave expiry +
    re-acquisition between the lock's release GET and DELETE)."""

    def __init__(self, inner):
        self._inner = inner
        self.on_get: Optional[GetHook] = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def get(self, key):
        val = await self._inner.get(key)
        if self.on_get is not None:
            await self.on_get(key, val)
        return val


@pytest.mark.asyncio
async def test_release_never_deletes_a_successors_lock():
    redis = fakeredis.aioredis.FakeRedis()
    hooked = HookedRedis(redis)

    async def expire_and_reacquire(key, val):
        hooked.on_get = None  # fire once, on A's release GET
        await redis.delete("lk")  # A's lock TTL expires...
        await redis.set("lk", b"token-B")  # ...and B acquires in the window

    # HookedRedis duck-types the two Redis calls the lock makes; it is deliberately not a Redis[Any].
    lock_a = RedisLock(hooked, "lk", ttl=30)  # pyright: ignore[reportArgumentType]
    async with lock_a as acquired:
        assert acquired
        hooked.on_get = expire_and_reacquire  # arm just before release

    assert (
        await redis.get("lk") == b"token-B"
    ), "A's release deleted B's lock: GET-then-DELETE must be an atomic compare-and-delete"
