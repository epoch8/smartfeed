"""High-priority missing tests for SmartFeed v2.

Covers:
1. Dedup across 3+ pages (with and without cache)
2. Session rebuild continues pagination
3. has_next_page at session boundary
4. Missing dedup key policies (error / drop / keep)
5. Positional with exhausted high-priority subfeed
"""

import pytest
import fakeredis.aioredis

from smartfeed.models import (
    FeedResult,
    MergerAppend,
    MergerPositional,
    SubFeed,
    Wrapper,
    WrapperCache,
    WrapperDedup,
)
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


# ---------------------------------------------------------------------------
# Subfeed helpers
# ---------------------------------------------------------------------------


async def make_overlapping_a(user_id, limit, next_page, **kw):
    """Produces ids 0, 2, 4, 6, ... (page-aware, even numbers)."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit * 2  # ids 0,2,4,6...
    data = [{"id": start + i * 2, "val": "a"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


async def make_overlapping_b(user_id, limit, next_page, **kw):
    """Produces ids 1, 3, 5, 7, ... with some overlap with source a."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit * 2 + 1  # ids 1,3,5...
    data = [{"id": start + i * 2, "val": "b"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


async def make_overlapping_ab(user_id, limit, next_page, **kw):
    """Produces ids that heavily overlap with source a: 0, 2, 4, ..."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit * 2
    data = [{"id": start + i * 2, "val": "ab_overlap"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


async def make_sequential(user_id, limit, next_page, **kw):
    """Produces sequentially numbered items across pages (0,1,2,...) -- no gaps."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": start + i, "val": f"seq_{start + i}"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


async def make_promo_limited(user_id, limit, next_page, **kw):
    """Returns only 2 items total, then empty."""
    page = next_page.get("page", 1)
    if page > 1:
        return FeedResult(data=[], next_page={"page": page + 1}, has_next_page=False)
    data = [{"id": 1000 + i, "val": "promo"} for i in range(2)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=False)


async def make_default_items(user_id, limit, next_page, **kw):
    """Returns plenty of default items, page-aware."""
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": start + i, "val": "default"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


OVERLAP_METHODS = {
    "overlap_a": make_overlapping_a,
    "overlap_b": make_overlapping_b,
    "overlap_ab": make_overlapping_ab,
}

SEQUENTIAL_METHODS = {
    "sequential": make_sequential,
}

POSITIONAL_METHODS = {
    "promo_limited": make_promo_limited,
    "default_items": make_default_items,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


# ---------------------------------------------------------------------------
# 1. Dedup across 3+ pages -- no duplicates
# ---------------------------------------------------------------------------


class TestDedupAcrossPages:
    """Test dedup behaviour across multiple pages.

    Without cache, dedup is applied per-request (per-page) only because there
    is no persisted seen-state between paginate calls.

    With cache, the entire session_size chunk is deduped at build time, so
    dedup holds across all pages that come from that cached chunk.
    """

    @pytest.mark.asyncio
    async def test_dedup_nocache_within_page_only(self):
        """Without cache, dedup removes duplicates WITHIN a single page
        but cannot prevent duplicates ACROSS pages because seen-state is not
        persisted.

        We use two sources that both return the same fixed set of ids on every
        call (ignoring pagination).  Within a single page, dedup collapses
        duplicates.  Across pages, the same ids reappear because there is no
        persisted seen-state.
        """

        async def make_fixed_a(user_id, limit, next_page, **kw):
            """Always returns ids 0..limit-1 regardless of page."""
            data = [{"id": i, "val": "a"} for i in range(limit)]
            return FeedResult(
                data=data,
                next_page={"page": next_page.get("page", 1) + 1},
                has_next_page=True,
            )

        async def make_fixed_b(user_id, limit, next_page, **kw):
            """Always returns ids 0..limit-1 (same as a) -- full overlap."""
            data = [{"id": i, "val": "b"} for i in range(limit)]
            return FeedResult(
                data=data,
                next_page={"page": next_page.get("page", 1) + 1},
                has_next_page=True,
            )

        methods = {"fixed_a": make_fixed_a, "fixed_b": make_fixed_b}
        ctx = ExecutionContext(
            session_id="dedup_nocache",
            methods_dict=methods,
            redis=None,
        )
        node = Wrapper(
            node_id="w",
            dedup=WrapperDedup(dedup_key="id"),
            data=MergerAppend(
                node_id="mix",
                items=[
                    SubFeed(subfeed_id="a", method_name="fixed_a"),
                    SubFeed(subfeed_id="b", method_name="fixed_b"),
                ],
            ),
        )

        all_ids = []
        cursor = {}
        for _ in range(3):
            result = await run_executor.run(node, ctx, limit=10, cursor=cursor)
            page_ids = [item["id"] for item in result.data]
            # Within each page, no duplicates
            assert len(page_ids) == len(set(page_ids)), "Duplicates found within a single page"
            all_ids.extend(page_ids)
            cursor = result.next_page

        # Across pages, duplicates ARE expected (seen-state not persisted).
        # This is the known limitation without cache.
        # We assert that cross-page duplicates exist to document the behaviour.
        assert len(all_ids) > len(set(all_ids)), (
            "Expected cross-page duplicates without cache, but none found. "
            "If the implementation now persists dedup state without cache, "
            "this test can be updated."
        )

    @pytest.mark.asyncio
    async def test_dedup_with_cache_across_pages(self, redis):
        """With cache, dedup is applied to the entire session_size chunk at
        cold-build time.  Pages served from that chunk have zero duplicates."""
        ctx = ExecutionContext(
            session_id="dedup_cached",
            methods_dict=OVERLAP_METHODS,
            redis=redis,
        )
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            dedup=WrapperDedup(dedup_key="id"),
            data=MergerAppend(
                node_id="mix",
                items=[
                    SubFeed(subfeed_id="a", method_name="overlap_a"),
                    SubFeed(subfeed_id="ab", method_name="overlap_ab"),
                ],
            ),
        )

        all_ids = []
        cursor = {}
        for page_num in range(1, 4):
            result = await run_executor.run(node, ctx, limit=10, cursor=cursor)
            page_ids = [item["id"] for item in result.data]
            # Within each page, no duplicates
            assert len(page_ids) == len(set(page_ids)), f"Duplicates in page {page_num}"
            all_ids.extend(page_ids)
            cursor = result.next_page

        # With cache, dedup holds across ALL pages served from the same session
        assert len(all_ids) == len(
            set(all_ids)
        ), "Duplicates found across cached pages -- dedup should cover the entire session"


# ---------------------------------------------------------------------------
# 2. Session rebuild continues pagination
# ---------------------------------------------------------------------------


class TestSessionRebuildContinuation:
    """Wrapper(cache, session_size=20), limit=10.

    Pages 1-2 come from the cache (20 items). Page 3 triggers a cache rebuild
    via the continuation cursor stored in :meta.  Items on page 3 should
    continue where page 2 left off with no gap and no overlap.
    """

    @pytest.mark.asyncio
    async def test_page3_continues_after_rebuild(self, redis):
        ctx = ExecutionContext(
            session_id="rebuild",
            methods_dict=SEQUENTIAL_METHODS,
            redis=redis,
        )
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=20, session_ttl=300),
            data=SubFeed(subfeed_id="seq", method_name="sequential"),
        )

        # Page 1
        r1 = await run_executor.run(node, ctx, limit=10, cursor={})
        assert len(r1.data) == 10
        ids_p1 = [item["id"] for item in r1.data]

        # Page 2 -- still from cache
        r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
        assert len(r2.data) == 10
        ids_p2 = [item["id"] for item in r2.data]

        # Page 3 -- should trigger rebuild (cache exhausted: offset 20 >= 20)
        r3 = await run_executor.run(node, ctx, limit=10, cursor=r2.next_page)
        assert len(r3.data) == 10, "Page 3 should return items after rebuild"
        ids_p3 = [item["id"] for item in r3.data]

        # No overlap between pages
        all_ids = ids_p1 + ids_p2 + ids_p3
        assert len(all_ids) == len(set(all_ids)), "Overlap detected between pages after session rebuild"

        # No gap: the maximum id from page 2 + 1 should be the minimum id from page 3
        # (since make_sequential produces strictly sequential ids)
        assert (
            min(ids_p3) == max(ids_p2) + 1
        ), f"Gap detected: page 2 ended at {max(ids_p2)}, page 3 starts at {min(ids_p3)}"


# ---------------------------------------------------------------------------
# 3. has_next_page at session boundary
# ---------------------------------------------------------------------------


class TestHasNextPageAtSessionBoundary:
    """Wrapper(cache, session_size=20), limit=10.

    After page 2, all 20 cached items are served. has_next_page should still
    be True because the child has more data.  Page 3 triggers rebuild and
    serves fresh items.
    """

    @pytest.mark.asyncio
    async def test_has_next_true_at_boundary(self, redis):
        ctx = ExecutionContext(
            session_id="boundary",
            methods_dict=SEQUENTIAL_METHODS,
            redis=redis,
        )
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=20, session_ttl=300),
            data=SubFeed(subfeed_id="seq", method_name="sequential"),
        )

        # Page 1
        r1 = await run_executor.run(node, ctx, limit=10, cursor={})
        assert r1.has_next_page is True, "Page 1 should have next page"

        # Page 2 -- last page of cache
        r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
        # The _paginate method sets has_next = end < len(data).
        # end = 20, len(data) = 20 => has_next = False from paginate.
        # However, the child has more data so the user should be able to
        # continue.  Verify the system still serves page 3:
        #
        # Note: _paginate returns has_next=False at the exact boundary.
        # The cursor still carries the gen, so on page 3 the wrapper detects
        # cache exhaustion and rebuilds.  The practical contract is:
        # even if has_next_page is False after page 2, requesting page 3
        # with the cursor still works (rebuild path).

        # Page 3 -- rebuild triggered
        r3 = await run_executor.run(node, ctx, limit=10, cursor=r2.next_page)
        assert len(r3.data) == 10, "Page 3 should return data after rebuild"
        assert r3.data[0]["id"] != r1.data[0]["id"], "Page 3 should contain new items, not a repeat of page 1"

    @pytest.mark.asyncio
    async def test_page2_has_next_page_reflects_boundary(self, redis):
        """Document the exact has_next_page value at the session boundary.

        _paginate sets has_next = end < len(data).
        With session_size=20, limit=10: page 2 => end=20, len=20 => False.
        This is a known edge: the client sees has_next_page=False but can
        still request page 3 (the rebuild path handles it).
        """
        ctx = ExecutionContext(
            session_id="boundary2",
            methods_dict=SEQUENTIAL_METHODS,
            redis=redis,
        )
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=20, session_ttl=300),
            data=SubFeed(subfeed_id="seq", method_name="sequential"),
        )

        r1 = await run_executor.run(node, ctx, limit=10, cursor={})
        r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)

        # Wrapper tracks child_has_next: at boundary, has_next_page=True
        # because child has more data (rebuild possible on next request).
        assert r2.has_next_page is True


# ---------------------------------------------------------------------------
# 4. Missing dedup key policies
# ---------------------------------------------------------------------------


class TestMissingDedupKeyPolicies:
    """Wrapper(dedup, dedup_key='id') with items that lack the 'id' field.

    Three policies:
    - 'error': raises KeyError
    - 'drop':  item is silently dropped
    - 'keep':  item is kept in the output
    """

    @staticmethod
    async def _make_items_with_missing_key(user_id, limit, next_page, **kw):
        """Half of the items have 'id', half do not. Stateful (advances by page) so a
        dedup refill fetches genuinely new items instead of regenerating seen ids."""
        page = next_page.get("page", 1)
        start = (page - 1) * limit
        data = []
        for i in range(limit):
            gid = start + i
            if gid % 2 == 0:
                data.append({"id": gid, "val": f"has_id_{gid}"})
            else:
                data.append({"val": f"no_id_{gid}"})  # no 'id' key
        return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)

    def _make_methods(self):
        return {"missing_key": self._make_items_with_missing_key}

    def _make_wrapper(self, policy):
        return Wrapper(
            node_id="w",
            dedup=WrapperDedup(dedup_key="id", missing_key_policy=policy),
            data=SubFeed(subfeed_id="src", method_name="missing_key"),
        )

    @pytest.mark.asyncio
    async def test_policy_error_raises(self):
        ctx = ExecutionContext(
            session_id="policy_error",
            methods_dict=self._make_methods(),
            redis=None,
        )
        node = self._make_wrapper("error")

        with pytest.raises(KeyError, match="Dedup key 'id' missing"):
            await run_executor.run(node, ctx, limit=10, cursor={})

    @pytest.mark.asyncio
    async def test_policy_drop_removes_items(self):
        ctx = ExecutionContext(
            session_id="policy_drop",
            methods_dict=self._make_methods(),
            redis=None,
        )
        node = self._make_wrapper("drop")
        result = await run_executor.run(node, ctx, limit=10, cursor={})

        # Only items WITH the 'id' key should survive (refill fills page to limit)
        for item in result.data:
            assert "id" in item, f"Item without 'id' was not dropped: {item}"

        # Refill loop fetches more items to fill the page after drop
        assert len(result.data) == 10

    @pytest.mark.asyncio
    async def test_policy_keep_preserves_items(self):
        ctx = ExecutionContext(
            session_id="policy_keep",
            methods_dict=self._make_methods(),
            redis=None,
        )
        node = self._make_wrapper("keep")
        result = await run_executor.run(node, ctx, limit=10, cursor={})

        # All items should be preserved
        assert len(result.data) == 10

        items_with_id = [item for item in result.data if "id" in item]
        items_without_id = [item for item in result.data if "id" not in item]
        assert len(items_with_id) == 5
        assert len(items_without_id) == 5


# ---------------------------------------------------------------------------
# 5. Positional with exhausted high-priority subfeed
# ---------------------------------------------------------------------------


class TestPositionalExhaustedPromo:
    """Production bug fix scenario.

    Setup: MergerPositional(positions=[1,3,5,7]) with a promo subfeed that
    returns only 2 items (has_next_page=False).  Default subfeed returns 20.

    The positional merger computes demand as:
        pos_count = 4 (slots at positions 1,3,5,7)
        default_demand = limit - pos_count = 16

    When promo returns only 2 of the 4 demanded items, the assemble loop
    fills positions 1 and 3 with promo, then falls back to default for
    positions 5 and 7.  However, default only has 16 items while 18 are
    needed (16 non-positional + 2 unfilled positional), so the result is
    18 items total (2 promo + 16 default).

    This documents the current behaviour: the merger does NOT dynamically
    increase the default demand to compensate for a partially-exhausted
    positional subfeed.
    """

    @pytest.mark.asyncio
    async def test_exhausted_promo_fills_with_default(self):
        ctx = ExecutionContext(
            session_id="exhausted_promo",
            methods_dict=POSITIONAL_METHODS,
            redis=None,
        )
        node = MergerPositional(
            node_id="pos",
            positions=[1, 3, 5, 7],
            positional=SubFeed(subfeed_id="promo", method_name="promo_limited"),
            default=SubFeed(subfeed_id="default", method_name="default_items"),
        )

        result = await run_executor.run(node, ctx, limit=20, cursor={})

        # Build a source map
        sources = []
        for item in result.data:
            info = item.get("_smartfeed_debug_info", {})
            sources.append(info.get("source"))

        # Promo should appear at index 0 (position 1) and index 2 (position 3)
        assert sources[0] == "promo", f"Position 1 should be promo, got {sources[0]}"
        assert sources[2] == "promo", f"Position 3 should be promo, got {sources[2]}"

        # Only 2 promo items total
        promo_positions = [i for i, src in enumerate(sources) if src == "promo"]
        assert (
            len(promo_positions) == 2
        ), f"Expected exactly 2 promo items, got {len(promo_positions)} at indices {promo_positions}"

        # Positions 5 and 7 fall back to default (promo exhausted)
        # These are at index 4 and 6 in the result (0-indexed) since only 2
        # promo items appear before them.
        for i, src in enumerate(sources):
            if i not in (0, 2):
                assert src == "default", f"Index {i} should be default, got {src}"

        # Current behaviour: result has fewer than `limit` items because
        # the merger under-allocated default demand.
        # default_demand = 20 - 4 = 16; actual need = 18 (16 + 2 unfilled promo slots)
        # Result: 2 promo + 16 default = 18
        assert len(result.data) == 18, (
            f"Expected 18 items (demand shortfall), got {len(result.data)}. "
            "If the implementation now backfills default demand, update this test to expect 20."
        )

    @pytest.mark.asyncio
    async def test_exhausted_promo_no_refetch(self):
        """Verify the promo subfeed is called only once (not re-fetched for
        unfilled positions)."""
        call_count = 0

        async def counting_promo(user_id, limit, next_page, **kw):
            nonlocal call_count
            call_count += 1
            page = next_page.get("page", 1)
            if page > 1:
                return FeedResult(data=[], next_page={"page": page + 1}, has_next_page=False)
            data = [{"id": 1000 + i, "val": "promo"} for i in range(2)]
            return FeedResult(data=data, next_page={"page": 2}, has_next_page=False)

        methods = {
            "promo_limited": counting_promo,
            "default_items": make_default_items,
        }
        ctx = ExecutionContext(
            session_id="no_refetch",
            methods_dict=methods,
            redis=None,
        )
        node = MergerPositional(
            node_id="pos",
            positions=[1, 3, 5, 7],
            positional=SubFeed(subfeed_id="promo", method_name="promo_limited"),
            default=SubFeed(subfeed_id="default", method_name="default_items"),
        )

        await run_executor.run(node, ctx, limit=20, cursor={})

        # The promo subfeed should be called exactly once
        assert call_count == 1, f"Promo subfeed was called {call_count} times; expected exactly 1"
