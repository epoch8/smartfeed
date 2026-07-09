"""Regression: cold-rebuild lock waiter serves the STALE cache batch.

When a rebuild races (double-fired page request at a cache-exhaustion boundary),
the lock loser polls `_read_meta` and accepts the FIRST meta it sees. The old
data/meta keys are never deleted before a rebuild, so the loser instantly serves
the OLD batch from offset 0 under the OLD gen: the user is re-shown page-1 items
mid-scroll, and the loser's next cursor is stale (the builder writes a new gen),
forcing a full feed restart on the following page.

Correct behavior: no concurrent response may repeat ids already shown earlier in
this scroll. (Both responses serving the same NEW page is fine -- same cursor,
idempotent retry.)

The child has latency 0.3s, so the winner is still building when the loser's
0.1s poll finds the stale meta -- the race is deterministic.
"""

import asyncio

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_concurrent_continuation_rebuild_does_not_reserve_shown_items():
    src = S.ScriptedSource(S.unique_pool(100), latency=0.3)
    node = S.wrapper(S.subfeed("src", "src"), session_size=20)
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
    shown = {it["id"] for it in r1.data} | {it["id"] for it in r2.data}

    # cursor offset == len(batch) -> continuation rebuild; double-fire it
    a, b = await asyncio.gather(
        run_executor.run(node, ctx, limit=10, cursor=r2.next_page),
        run_executor.run(node, ctx, limit=10, cursor=r2.next_page),
    )
    ids_a = [it["id"] for it in a.data]
    ids_b = [it["id"] for it in b.data]

    assert not (set(ids_a) & shown), f"response A re-served already-shown ids: {sorted(set(ids_a) & shown)}"
    assert not (set(ids_b) & shown), f"response B re-served already-shown ids: {sorted(set(ids_b) & shown)}"
