"""Regression: `_touch_ttl` refreshes cache data+meta but never
the dedup seen-set.

Every warm page read refreshes the TTL on the data and :meta keys (inactivity
timeout), but the :seen key keeps its original state_ttl countdown. A slow
scroll therefore keeps the cache alive indefinitely while the seen-set silently
expires; the next continuation rebuild then loads an empty seen-set and
re-serves already-shown ids, breaking the documented "each item at most once
per scroll" contract.

Correct behavior: a warm read that refreshes data/meta must refresh :seen too
(it is the same inactivity signal, and dedup state must not outlive-lag the
cache it guards).
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_warm_read_refreshes_seen_set_ttl_alongside_cache():
    redis = fakeredis.aioredis.FakeRedis()
    src = S.ScriptedSource(S.unique_pool(30))
    node = S.wrapper(S.subfeed("src", "src"), session_size=10, dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=redis)

    r1 = await run_executor.run(node, ctx, limit=5, cursor={})  # cold build

    # Age every key close to expiry, then serve a warm page.
    keys = sorted(k.decode() for k in await redis.keys("*"))
    seen_keys = [k for k in keys if k.endswith(":seen")]
    assert seen_keys, "dedup wrapper must have written a :seen key"
    for k in keys:
        await redis.expire(k, 7)

    await run_executor.run(node, ctx, limit=5, cursor=r1.next_page)  # warm hit

    # Control: data/meta are refreshed today (documented inactivity-timeout behavior).
    base_key = [k for k in keys if not k.endswith((":seen", ":meta"))][0]
    assert await redis.ttl(base_key) > 7, "warm read should refresh the cache TTL"

    # The bug: :seen stays on its old countdown while the cache it guards lives on.
    seen_ttl = await redis.ttl(seen_keys[0])
    assert seen_ttl > 7, (
        f":seen TTL was not refreshed on the warm read (still {seen_ttl}s) -- "
        f"the seen-set can expire mid-scroll while the cache stays alive, causing duplicates"
    )
