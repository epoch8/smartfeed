"""BUG #1 -- dedup silently drops the overfetched remainder.

Both dedup paths fetch ``target * overfetch_factor`` items, keep ``target``, and
return the child cursor advanced past *all* fetched items. The surviving-but-
truncated items are consumed from the source and never served on any page.

  * _fetch_and_dedup (cached):   wrapper.py:456 (fetch_size) + :479 (data[:target])
  * _passthrough     (no cache): wrapper.py:217 (fetch_size) + :234 (data[:limit])

These tests drain a finite source and assert full coverage. They fail today
(~75% of items lost at overfetch=4) and must pass once the remainder is
preserved instead of discarded.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S


def _redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.mark.asyncio
async def test_cached_dedup_covers_all_unique_items():
    src = S.ScriptedSource(S.unique_pool(200))
    node = S.wrapper(S.subfeed("src", "src"), session_size=20,
                     dedup_key="id", overfetch_factor=4)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=200)
    S.assert_full_coverage(pages, set(range(200)))


@pytest.mark.asyncio
async def test_passthrough_dedup_covers_all_unique_items():
    src = S.ScriptedSource(S.unique_pool(200))
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id", overfetch_factor=4)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=200)
    S.assert_full_coverage(pages, set(range(200)))


@pytest.mark.asyncio
async def test_cached_dedup_covers_all_with_real_duplicates():
    """With a source that genuinely contains duplicates, dedup should remove the
    dupes but still surface every *unique* id. Coverage = the unique universe."""
    pool = S.dup_pool(150, every=4)  # unique universe is range(150), plus cross-page dupes
    src = S.ScriptedSource(pool)
    node = S.wrapper(S.subfeed("src", "src"), session_size=20,
                     dedup_key="id", overfetch_factor=4)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_full_coverage(pages, set(range(150)))


@pytest.mark.asyncio
async def test_overfetch_1_is_lossless_control():
    """Control: with overfetch_factor=1 there is no discard, so no loss. Proves the
    harness is sound and localizes the bug to overfetch>1."""
    src = S.ScriptedSource(S.unique_pool(200))
    node = S.wrapper(S.subfeed("src", "src"), session_size=20,
                     dedup_key="id", overfetch_factor=1)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=300)
    S.assert_full_coverage(pages, set(range(200)))


@pytest.mark.asyncio
async def test_no_dedup_is_lossless_control():
    """Control: a plain cached wrapper (no dedup) never overfetches, never loses."""
    src = S.ScriptedSource(S.unique_pool(200))
    node = S.wrapper(S.subfeed("src", "src"), session_size=20)
    ctx = S.make_ctx({"src": src}, redis=_redis())
    pages = await S.drain(node, ctx, limit=10, max_pages=200)
    S.assert_full_coverage(pages, set(range(200)))
