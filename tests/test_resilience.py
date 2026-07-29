"""Tests for resilience: source failures, positional accuracy, rerank error handling."""

import pytest
import fakeredis.aioredis

from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.wrapper import Wrapper, WrapperCache, WrapperRerank, WrapperDedup
from smartfeed.models.mixers import MergerPercentage, MergerPercentageItem, MergerPositional
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def make_source(prefix, user_id, limit, next_page, **kwargs):
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": f"{prefix}_{start + i}", "val": prefix} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": page + 1}, has_next_page=True)


async def make_failing_source(user_id, limit, next_page, **kwargs):
    raise RuntimeError("source unavailable")


async def make_promo(user_id, limit, next_page, **kwargs):
    data = [{"id": f"promo_{i}", "val": "promo"} for i in range(limit)]
    return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)


async def rerank_reverse(items, session_id):
    return list(reversed(items))


async def rerank_crash(items, session_id):
    raise RuntimeError("ML service down")


METHODS = {
    "source_a": lambda *a, **kw: make_source("a", *a, **kw),
    "source_b": lambda *a, **kw: make_source("b", *a, **kw),
    "failing": make_failing_source,
    "promo": make_promo,
    "rerank_ok": rerank_reverse,
    "rerank_crash": rerank_crash,
}


@pytest.fixture
def redis():
    return fakeredis.aioredis.FakeRedis()


@pytest.fixture
def ctx(redis):
    return ExecutionContext(session_id="test", methods_dict=METHODS, redis=redis)


# ---------------------------------------------------------------------------
# 1. Source failure: fill page from remaining source
# ---------------------------------------------------------------------------


class TestSourceFailureFallback:
    @pytest.mark.asyncio
    async def test_one_source_fails_page_filled_from_other(self, ctx):
        """If one source in percentage mix fails, the other fills the page."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            data=MergerPercentage(
                node_id="mix",
                items=[
                    MergerPercentageItem(
                        percentage=50,
                        data=SubFeed(subfeed_id="good", method_name="source_a"),
                    ),
                    MergerPercentageItem(
                        percentage=50,
                        data=SubFeed(subfeed_id="bad", method_name="failing", raise_error=False),
                    ),
                ],
            ),
        )
        result = await run_executor.run(node, ctx, limit=20, cursor={})
        # Should have items from source_a only (source_b failed gracefully)
        assert len(result.data) > 0
        for item in result.data:
            assert item["val"] == "a"

    @pytest.mark.asyncio
    async def test_all_sources_fail_returns_empty(self, ctx):
        """If all sources fail, return empty gracefully."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            data=MergerPercentage(
                node_id="mix",
                items=[
                    MergerPercentageItem(
                        percentage=50,
                        data=SubFeed(subfeed_id="bad1", method_name="failing", raise_error=False),
                    ),
                    MergerPercentageItem(
                        percentage=50,
                        data=SubFeed(subfeed_id="bad2", method_name="failing", raise_error=False),
                    ),
                ],
            ),
        )
        result = await run_executor.run(node, ctx, limit=20, cursor={})
        assert result.data == []


# ---------------------------------------------------------------------------
# 2. Positional: strict position enforcement
# ---------------------------------------------------------------------------


class TestPositionalStrictPositions:
    @pytest.mark.asyncio
    async def test_promo_at_exact_positions(self, ctx):
        """Promo items must be at positions 1, 3, 5, 7 (1-indexed)."""
        node = MergerPositional(
            node_id="pos",
            positions=[1, 3, 5, 7],
            positional=SubFeed(subfeed_id="promo", method_name="promo"),
            default=SubFeed(subfeed_id="regular", method_name="source_a"),
        )
        result = await run_executor.run(node, ctx, limit=20, cursor={})
        assert len(result.data) == 20

        for pos in [1, 3, 5, 7]:
            item = result.data[pos - 1]  # 1-indexed -> 0-indexed
            source = item.get("_smartfeed_debug_info", {}).get("source", "")
            assert source == "promo", f"Position {pos}: expected promo, got {source} (id={item.get('id')})"

    @pytest.mark.asyncio
    async def test_non_promo_positions_filled_by_default(self, ctx):
        """Non-promo positions must be filled by default source."""
        node = MergerPositional(
            node_id="pos",
            positions=[1, 3, 5, 7],
            positional=SubFeed(subfeed_id="promo", method_name="promo"),
            default=SubFeed(subfeed_id="regular", method_name="source_a"),
        )
        result = await run_executor.run(node, ctx, limit=20, cursor={})

        non_promo_positions = [i for i in range(20) if (i + 1) not in [1, 3, 5, 7]]
        for pos in non_promo_positions:
            item = result.data[pos]
            source = item.get("_smartfeed_debug_info", {}).get("source", "")
            assert source == "regular", f"Position {pos + 1}: expected regular, got {source}"

    @pytest.mark.asyncio
    async def test_positional_with_dedup_preserves_positions(self, ctx):
        """Even with outer dedup, promo positions must be preserved."""
        node = Wrapper(
            node_id="dedup_outer",
            dedup=WrapperDedup(dedup_key="id"),
            data=MergerPositional(
                node_id="pos",
                positions=[1, 3, 5, 7],
                positional=SubFeed(subfeed_id="promo", method_name="promo", dedup_priority=10),
                default=SubFeed(subfeed_id="regular", method_name="source_a", dedup_priority=1),
            ),
        )
        result = await run_executor.run(node, ctx, limit=20, cursor={})
        assert len(result.data) == 20

        for pos in [1, 3, 5, 7]:
            item = result.data[pos - 1]
            source = item.get("_smartfeed_debug_info", {}).get("source", "")
            assert source == "promo", f"Position {pos}: expected promo after dedup, got {source}"


# ---------------------------------------------------------------------------
# 3. Rerank failure: raise_error=True vs False
# ---------------------------------------------------------------------------


class TestRerankFailureHandling:
    @pytest.mark.asyncio
    async def test_rerank_crash_raise_error_true(self, ctx):
        """With raise_error=True (default), rerank failure crashes the request."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            rerank=WrapperRerank(method_name="rerank_crash", raise_error=True),
            data=SubFeed(subfeed_id="src", method_name="source_a"),
        )
        with pytest.raises(RuntimeError, match="ML service down"):
            await run_executor.run(node, ctx, limit=10, cursor={})

    @pytest.mark.asyncio
    async def test_rerank_crash_raise_error_false(self, ctx):
        """With raise_error=False, rerank failure keeps original order."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            rerank=WrapperRerank(method_name="rerank_crash", raise_error=False),
            data=SubFeed(subfeed_id="src", method_name="source_a"),
        )
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        assert len(result.data) == 10
        # Items should be in original order (not reversed, since rerank_crash failed)
        ids = [item["id"] for item in result.data]
        assert ids[0] == "a_0"  # original order preserved

    @pytest.mark.asyncio
    async def test_rerank_ok_actually_reranks(self, ctx):
        """Verify working rerank changes order (control test)."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            rerank=WrapperRerank(method_name="rerank_ok"),
            data=SubFeed(subfeed_id="src", method_name="source_a"),
        )
        result = await run_executor.run(node, ctx, limit=10, cursor={})
        assert len(result.data) == 10
        # rerank_ok reverses, so last item of original is first
        ids = [item["id"] for item in result.data]
        assert ids[0] != "a_0"  # order changed

    @pytest.mark.asyncio
    async def test_rerank_fail_soft_page2_still_works(self, ctx):
        """After fail-soft rerank, page 2 from cache still works."""
        node = Wrapper(
            node_id="w",
            cache=WrapperCache(session_size=50, session_ttl=300),
            rerank=WrapperRerank(method_name="rerank_crash", raise_error=False),
            data=SubFeed(subfeed_id="src", method_name="source_a"),
        )
        r1 = await run_executor.run(node, ctx, limit=10, cursor={})
        r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
        assert len(r1.data) == 10
        assert len(r2.data) == 10
        # Different pages
        ids1 = {item["id"] for item in r1.data}
        ids2 = {item["id"] for item in r2.data}
        assert not ids1 & ids2  # no overlap
