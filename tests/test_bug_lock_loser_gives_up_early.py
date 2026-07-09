"""Regression: the cold-build lock loser gives up after 5s of
polling while the winner's lock lives 30s.

`_cold_build_locked` polls 50 x 0.1s and then falls through to an UNLOCKED
`_cold_build`. Any cold build slower than 5s therefore gets a second, unguarded
child fetch racing the real builder: double upstream load and two competing
gens, one of which is orphaned (that client's next page triggers yet another
full rebuild).

Correct behavior: the loser waits out the winner (poll budget >= lock TTL, or
re-attempt the lock) so the child is fetched exactly once.

Timing is compressed uniformly (all asyncio.sleep x0.1) so the >5s-nominal
build finishes in <1s of real time; the poll-budget/build-time ratio that
triggers the bug is preserved.
"""

import asyncio

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_slow_cold_build_still_fetches_child_once(monkeypatch):
    real_sleep = asyncio.sleep

    async def compressed_sleep(delay: float) -> None:
        await real_sleep(delay * 0.1)

    monkeypatch.setattr(asyncio, "sleep", compressed_sleep)

    # Nominal 8s child latency: longer than the old code's 5s poll budget,
    # comfortably shorter than the 10s lock TTL the fixed code waits out.
    src = S.ScriptedSource(S.unique_pool(100), latency=8.0)
    node = S.wrapper(S.subfeed("src", "src"), session_size=50)
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    a, b = await asyncio.gather(
        run_executor.run(node, ctx, limit=10, cursor={}),
        run_executor.run(node, ctx, limit=10, cursor={}),
    )

    assert src.calls == 1, (
        f"expected 1 cold fetch (loser waits for the lock holder), got {src.calls}: "
        f"the loser gave up polling before the winner finished and rebuilt without the lock"
    )
    gens = {a.next_page["w"]["gen"], b.next_page["w"]["gen"]}
    assert len(gens) == 1, f"concurrent callers got competing generations: {gens}"
