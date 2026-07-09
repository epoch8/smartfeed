"""Two wrappers sharing a cache_key race one cold build: the shared-segment lock
must serialize them so the child is fetched exactly once (the _build_shared_base
waiter path had no concurrent coverage).

The child has latency, so both wrappers are guaranteed to be inside the cold
build window together; their own coldlocks differ (different wrapper config
hashes), so contention lands on the shared-segment lock itself.
"""

import asyncio

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


async def identity(items, session_id):
    return items


async def reverse(items, session_id):
    return list(reversed(items))


@pytest.mark.asyncio
async def test_concurrent_shared_cold_build_fetches_child_once():
    src = S.ScriptedSource(S.unique_pool(100), latency=0.3)
    ctx = S.make_ctx({"src": src, "identity": identity, "reverse": reverse}, redis=fakeredis.aioredis.FakeRedis())
    w_a = S.wrapper(S.subfeed("src", "src"), node_id="a", session_size=20, cache_key="pool", rerank_method="identity")
    w_b = S.wrapper(S.subfeed("src", "src"), node_id="b", session_size=20, cache_key="pool", rerank_method="reverse")

    a, b = await asyncio.gather(
        run_executor.run(w_a, ctx, limit=10, cursor={}),
        run_executor.run(w_b, ctx, limit=10, cursor={}),
    )

    assert src.calls == 1, f"shared base must be fetched once, got {src.calls}"
    assert len(a.data) == 10 and len(b.data) == 10
    # Same shared base, per-wrapper rerank: b serves a's window reversed-ish (both
    # windows come from the same 20-item segment).
    assert {it["id"] for it in a.data} | {it["id"] for it in b.data} <= set(range(20))
