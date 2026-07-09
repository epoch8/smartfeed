"""Regression: shared cache_key path drops cross-round dedup.

`_cold_build` (shared branch) calls `_fetch_and_dedup` with seen=None, and
`_dedup` then allocates a FRESH seen set on every refill round (wrapper.py:121).
Round 2's refill re-admits ids already collected in round 1, so duplicates
survive WITHIN one shared segment and are served to the user -- contradicting
README's "the shared base dedups within each segment".

The identical config without cache_key dedups correctly (control test).
"""

import pytest
import fakeredis.aioredis

from tests import sources as S


POOL = S.dup_pool(20, every=4)  # earlier ids re-emitted -> duplicates across refill rounds


@pytest.mark.asyncio
async def test_shared_cache_scroll_has_no_duplicates():
    src = S.ScriptedSource(POOL)
    node = S.wrapper(S.subfeed("src", "src"), session_size=10, dedup_key="id", cache_key="shared")
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    pages = await S.drain(node, ctx, limit=5)
    S.assert_no_duplicates(pages)


@pytest.mark.asyncio
async def test_standard_cache_scroll_has_no_duplicates_control():
    """Control: same config minus cache_key is clean today."""
    src = S.ScriptedSource(POOL)
    node = S.wrapper(S.subfeed("src", "src"), session_size=10, dedup_key="id")
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())

    pages = await S.drain(node, ctx, limit=5)
    S.assert_no_duplicates(pages)
