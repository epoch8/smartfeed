"""BUG #5 -- cold-build thundering herd on the standard (non-shared) cached path.

Only the cache_key path takes a RedisLock. For a normal cached wrapper, N
concurrent first requests for one session each fetch the full session_size from
the child and each write a different gen; last writer wins, so the losers' gens
no longer match meta and they rebuild again on their next page. Upstream
amplification + cache thrash.

The source has latency so all N callers enter the cold build before any of them
writes the cache -- a deterministic race. Fails today (N fetches); passes once
the standard path is guarded like the shared path.
"""

import asyncio

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


N = 5


@pytest.mark.asyncio
async def test_concurrent_cold_build_fetches_child_once():
    src = S.ScriptedSource(S.unique_pool(200), latency=0.05)
    node = S.wrapper(S.subfeed("src", "src"), session_size=50)
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    results = await asyncio.gather(*[
        run_executor.run(node, ctx, limit=10, cursor={}) for _ in range(N)
    ])

    assert all(len(r.data) == 10 for r in results)
    # One winner should fetch the child; the rest should read the cache it wrote.
    assert src.calls == 1, f"expected 1 cold fetch, got {src.calls} (thundering herd)"


@pytest.mark.asyncio
async def test_concurrent_cold_build_single_generation():
    src = S.ScriptedSource(S.unique_pool(200), latency=0.05)
    node = S.wrapper(S.subfeed("src", "src"), session_size=50)
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    results = await asyncio.gather(*[
        run_executor.run(node, ctx, limit=10, cursor={}) for _ in range(N)
    ])
    gens = {r.next_page["w"]["gen"] for r in results}
    assert len(gens) == 1, f"concurrent callers got {len(gens)} different generations: {gens}"


@pytest.mark.asyncio
async def test_shared_cache_path_already_locks_control():
    """Control: the cache_key path DOES take a lock, so it fetches once."""
    src = S.ScriptedSource(S.unique_pool(200), latency=0.05)
    node = S.wrapper(S.subfeed("src", "src"), node_id="w",
                     session_size=50, cache_key="shared")
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())
    await asyncio.gather(*[
        run_executor.run(node, ctx, limit=10, cursor={}) for _ in range(N)
    ])
    assert src.calls == 1, f"shared path should fetch once, got {src.calls}"
