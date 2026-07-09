"""Regression: passthrough dedup never resets the seen-set on a
fresh scroll.

Documented contract (README "Dedup contract", CHANGELOG 1.0.0): "re-sending an
empty cursor (or page 1) starts a FRESH scroll and may repeat items already
shown in a previous scroll". The cached path honors this (`_reset_seen_set` in
`_cold_build`); the passthrough path only ever loads and appends to the
seen-set, so a page refresh (empty cursor) silently filters out everything the
previous scroll showed instead of serving page 1 again.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.execution import executor as run_executor


@pytest.mark.asyncio
async def test_empty_cursor_starts_fresh_scroll_on_passthrough():
    src = S.ScriptedSource(S.unique_pool(50))
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id")  # no cache -> passthrough
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    r1 = await run_executor.run(node, ctx, limit=10, cursor={})
    ids_scroll_1 = [it["id"] for it in r1.data]

    # New scroll: empty cursor again (page refresh / F5)
    r2 = await run_executor.run(node, ctx, limit=10, cursor={})
    ids_scroll_2 = [it["id"] for it in r2.data]

    assert ids_scroll_2 == ids_scroll_1, (
        f"fresh scroll must restart from page 1, got {ids_scroll_2} " f"(previous scroll's seen-set was never reset)"
    )
