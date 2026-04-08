"""Medium-priority missing tests for SmartFeed v2.

Covers:
1. SubFeed with subfeed_params forwarding
2. MergerAppend cursor pagination across two pages
3. MergerAppend with one empty child
4. MergerPercentage with one empty source
5. MergerPercentage odd limit (11) -- no off-by-one
6. MergerPositional step-based positions (start=2, end=20, step=3)
7. MergerPositional with empty default subfeed
"""

import pytest

from smartfeed.models import (
    FeedResult,
    MergerAppend,
    MergerPercentage,
    MergerPercentageItem,
    MergerPositional,
    SubFeed,
)
from smartfeed.execution.context import ExecutionContext
from smartfeed.execution import executor as run_executor
from tests.conftest import METHODS


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(methods):
    return ExecutionContext(session_id="s1", methods_dict=methods, redis=None)


# ---------------------------------------------------------------------------
# 1. SubFeed with subfeed_params forwarding
# ---------------------------------------------------------------------------


class TestSubFeedParamsForwarding:
    """SubFeed.subfeed_params must be forwarded to the underlying method."""

    @pytest.mark.asyncio
    async def test_custom_param_received_by_method(self):
        async def make_with_param(user_id, limit, next_page, custom_param=None, **kw):
            assert custom_param == 42
            data = [{"id": i, "param": custom_param} for i in range(limit)]
            return FeedResult(data=data, next_page={"page": 2}, has_next_page=True)

        methods = {"with_param": make_with_param}
        ctx = _ctx(methods)
        node = SubFeed(
            subfeed_id="src",
            method_name="with_param",
            subfeed_params={"custom_param": 42},
        )

        result = await run_executor.run(node, ctx, limit=5, cursor={})

        assert len(result.data) == 5
        for item in result.data:
            assert item["param"] == 42

    @pytest.mark.asyncio
    async def test_custom_param_value_is_correct(self):
        """Verify the exact value 42 is forwarded, not a default or None."""
        received = {}

        async def capture_param(user_id, limit, next_page, custom_param=None, **kw):
            received["custom_param"] = custom_param
            return FeedResult(
                data=[{"id": 0}],
                next_page={"page": 2},
                has_next_page=False,
            )

        methods = {"capture": capture_param}
        ctx = _ctx(methods)
        node = SubFeed(
            subfeed_id="src",
            method_name="capture",
            subfeed_params={"custom_param": 42},
        )

        await run_executor.run(node, ctx, limit=1, cursor={})

        assert received.get("custom_param") == 42


# ---------------------------------------------------------------------------
# 2. MergerAppend cursor pagination
# ---------------------------------------------------------------------------


class TestMergerAppendCursorPagination:
    """MergerAppend must propagate cursors so page 2 returns different items."""

    @pytest.mark.asyncio
    async def test_page2_returns_different_items(self):
        ctx = _ctx(METHODS)
        node = MergerAppend(
            node_id="append",
            items=[
                SubFeed(subfeed_id="a", method_name="items"),
                SubFeed(subfeed_id="b", method_name="items"),
            ],
        )

        r1 = await run_executor.run(node, ctx, limit=10, cursor={})
        assert len(r1.data) == 10

        r2 = await run_executor.run(node, ctx, limit=10, cursor=r1.next_page)
        assert len(r2.data) == 10

        ids_p1 = {item["id"] for item in r1.data}
        ids_p2 = {item["id"] for item in r2.data}

        # Page 2 items must differ from page 1 items
        assert ids_p1 != ids_p2, "Page 2 returned the same items as page 1"

    @pytest.mark.asyncio
    async def test_cursor_carries_both_child_cursors(self):
        """The merged cursor must contain entries for both child subfeeds."""
        ctx = _ctx(METHODS)
        node = MergerAppend(
            node_id="append",
            items=[
                SubFeed(subfeed_id="a", method_name="items"),
                SubFeed(subfeed_id="b", method_name="items"),
            ],
        )

        r1 = await run_executor.run(node, ctx, limit=10, cursor={})

        # Both subfeed cursors must be present so page 2 can continue correctly
        assert "a" in r1.next_page, "Cursor is missing child 'a'"
        assert "b" in r1.next_page, "Cursor is missing child 'b'"


# ---------------------------------------------------------------------------
# 3. MergerAppend with one empty child
# ---------------------------------------------------------------------------


class TestMergerAppendOneEmptyChild:
    """When one child returns empty, output must contain items from the other."""

    @pytest.mark.asyncio
    async def test_result_contains_non_empty_child_items(self):
        ctx = _ctx(METHODS)
        node = MergerAppend(
            node_id="append",
            items=[
                SubFeed(subfeed_id="a", method_name="items"),
                SubFeed(subfeed_id="empty", method_name="empty"),
            ],
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        assert len(result.data) > 0, "Expected items from the non-empty child"
        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        assert all(src == "a" for src in sources), (
            f"Expected all items from 'a', got sources: {sources}"
        )

    @pytest.mark.asyncio
    async def test_has_next_page_reflects_non_empty_child(self):
        """has_next_page should be True because the non-empty child has more data."""
        ctx = _ctx(METHODS)
        node = MergerAppend(
            node_id="append",
            items=[
                SubFeed(subfeed_id="a", method_name="items"),
                SubFeed(subfeed_id="empty", method_name="empty"),
            ],
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        assert result.has_next_page is True


# ---------------------------------------------------------------------------
# 4. MergerPercentage with one empty source
# ---------------------------------------------------------------------------


class TestMergerPercentageOneEmptySource:
    """50/50 split: when one source is empty, all output comes from the other."""

    @pytest.mark.asyncio
    async def test_result_contains_only_non_empty_source(self):
        ctx = _ctx(METHODS)
        node = MergerPercentage(
            node_id="pct",
            items=[
                MergerPercentageItem(
                    percentage=50,
                    data=SubFeed(subfeed_id="items", method_name="items"),
                ),
                MergerPercentageItem(
                    percentage=50,
                    data=SubFeed(subfeed_id="empty", method_name="empty"),
                ),
            ],
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        assert len(result.data) > 0, "Expected items from the non-empty source"
        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        assert all(src == "items" for src in sources), (
            f"Expected all items from 'items', got sources: {sources}"
        )

    @pytest.mark.asyncio
    async def test_no_items_from_empty_source(self):
        ctx = _ctx(METHODS)
        node = MergerPercentage(
            node_id="pct",
            items=[
                MergerPercentageItem(
                    percentage=50,
                    data=SubFeed(subfeed_id="empty", method_name="empty"),
                ),
                MergerPercentageItem(
                    percentage=50,
                    data=SubFeed(subfeed_id="items", method_name="items"),
                ),
            ],
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        assert "empty" not in sources, "Got items from the empty source"


# ---------------------------------------------------------------------------
# 5. MergerPercentage odd limit (11) -- no off-by-one
# ---------------------------------------------------------------------------


class TestMergerPercentageOddLimit:
    """40/60 split with limit=11 must return exactly 11 items."""

    @pytest.mark.asyncio
    async def test_odd_limit_returns_exactly_11(self):
        ctx = _ctx(METHODS)
        node = MergerPercentage(
            node_id="pct",
            items=[
                MergerPercentageItem(
                    percentage=40,
                    data=SubFeed(subfeed_id="a", method_name="items"),
                ),
                MergerPercentageItem(
                    percentage=60,
                    data=SubFeed(subfeed_id="b", method_name="items"),
                ),
            ],
        )

        result = await run_executor.run(node, ctx, limit=11, cursor={})

        assert len(result.data) == 11, (
            f"Expected exactly 11 items for odd limit with 40/60 split, got {len(result.data)}"
        )

    @pytest.mark.asyncio
    async def test_odd_limit_split_sums_to_limit(self):
        """The demands for 40% and 60% of 11 must sum to 11 (4 + 7 = 11)."""
        ctx = _ctx(METHODS)
        node = MergerPercentage(
            node_id="pct",
            items=[
                MergerPercentageItem(
                    percentage=40,
                    data=SubFeed(subfeed_id="a", method_name="items"),
                ),
                MergerPercentageItem(
                    percentage=60,
                    data=SubFeed(subfeed_id="b", method_name="items"),
                ),
            ],
        )

        result = await run_executor.run(node, ctx, limit=11, cursor={})

        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        count_a = sources.count("a")
        count_b = sources.count("b")

        assert count_a + count_b == 11
        # 40% of 11 = 4.4 -> floor 4, remainder 0.4; 60% of 11 = 6.6 -> floor 6, remainder 0.6
        # Remainder distribution gives the 1 leftover to 'b' (highest remainder 60)
        assert count_a == 4, f"Expected 4 items from 'a' (40% of 11), got {count_a}"
        assert count_b == 7, f"Expected 7 items from 'b' (60% of 11), got {count_b}"


# ---------------------------------------------------------------------------
# 6. MergerPositional step-based positions
# ---------------------------------------------------------------------------


class TestMergerPositionalStepBased:
    """Positions generated via range(start=2, stop=21, step=3) = [2,5,8,11,14,17,20].

    Items at those 1-indexed positions must come from the positional subfeed.
    """

    _STEP_POSITIONS = list(range(2, 21, 3))  # [2, 5, 8, 11, 14, 17, 20]

    @pytest.mark.asyncio
    async def test_positional_items_at_step_positions(self):
        ctx = _ctx(METHODS)
        node = MergerPositional(
            node_id="pos",
            positions=self._STEP_POSITIONS,
            positional=SubFeed(subfeed_id="positional", method_name="items"),
            default=SubFeed(subfeed_id="default", method_name="items"),
        )

        result = await run_executor.run(node, ctx, limit=20, cursor={})
        assert len(result.data) == 20

        pos_set = set(self._STEP_POSITIONS)
        for i, item in enumerate(result.data):
            position = i + 1  # 1-indexed
            source = item["_smartfeed_debug_info"]["source"]
            if position in pos_set:
                assert source == "positional", (
                    f"Position {position} (index {i}) should be 'positional', got '{source}'"
                )
            else:
                assert source == "default", (
                    f"Position {position} (index {i}) should be 'default', got '{source}'"
                )

    @pytest.mark.asyncio
    async def test_step_positions_count(self):
        """Verify exactly 7 positional items appear in the output."""
        ctx = _ctx(METHODS)
        node = MergerPositional(
            node_id="pos",
            positions=self._STEP_POSITIONS,
            positional=SubFeed(subfeed_id="positional", method_name="items"),
            default=SubFeed(subfeed_id="default", method_name="items"),
        )

        result = await run_executor.run(node, ctx, limit=20, cursor={})

        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        assert sources.count("positional") == len(self._STEP_POSITIONS), (
            f"Expected {len(self._STEP_POSITIONS)} positional items, "
            f"got {sources.count('positional')}"
        )
        assert sources.count("default") == 20 - len(self._STEP_POSITIONS)


# ---------------------------------------------------------------------------
# 7. MergerPositional with empty default
# ---------------------------------------------------------------------------


class TestMergerPositionalEmptyDefault:
    """When the default subfeed is empty, only positional items appear."""

    @pytest.mark.asyncio
    async def test_only_positional_items_when_default_empty(self):
        ctx = _ctx(METHODS)
        positions = [1, 3, 5]
        node = MergerPositional(
            node_id="pos",
            positions=positions,
            positional=SubFeed(subfeed_id="positional", method_name="items"),
            default=SubFeed(subfeed_id="empty_default", method_name="empty"),
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        assert len(result.data) > 0, "Expected positional items in the result"
        sources = [item["_smartfeed_debug_info"]["source"] for item in result.data]
        assert all(src == "positional" for src in sources), (
            f"Expected all items from 'positional', got sources: {sources}"
        )

    @pytest.mark.asyncio
    async def test_positional_items_fill_configured_positions(self):
        """With empty default, positional items still land at the right positions."""
        ctx = _ctx(METHODS)
        positions = [2, 4]
        node = MergerPositional(
            node_id="pos",
            positions=positions,
            positional=SubFeed(subfeed_id="positional", method_name="items"),
            default=SubFeed(subfeed_id="empty_default", method_name="empty"),
        )

        result = await run_executor.run(node, ctx, limit=10, cursor={})

        # With no default items, the assemble loop skips non-positional slots
        # and only places items at the configured positional slots.
        # Positional items appear but non-positional slots are vacant.
        pos_set = set(positions)
        for i, item in enumerate(result.data):
            source = item["_smartfeed_debug_info"]["source"]
            assert source == "positional", (
                f"Index {i} should be 'positional' (only source), got '{source}'"
            )
