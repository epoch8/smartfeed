import inspect

import pytest

from smartfeed.schemas import (
    FeedResultClient,
    FeedResultNextPage,
    FeedResultNextPageInside,
    MergerDeduplication,
)

from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


def make_offset_paged_method(items, *, max_per_call=None):
    async def _method(user_id, limit, next_page, **kwargs):  # pylint: disable=unused-argument
        offset = int(next_page.after or 0)
        effective_limit = limit
        if isinstance(max_per_call, int) and max_per_call > 0:
            effective_limit = min(effective_limit, max_per_call)
        result_data = items[offset : offset + effective_limit]
        next_page.after = offset + len(result_data)
        next_page.page += 1
        has_next_page = (offset + len(result_data)) < len(items)
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=has_next_page)

    return _method


def _assert_cursor_monotonic_if_present(res_1, res_2, keys):
    for key in keys:
        if key not in res_1.next_page.data:
            continue

        assert key in res_2.next_page.data

        after_1 = res_1.next_page.data[key].after
        after_2 = res_2.next_page.data[key].after

        if after_1 is None or after_2 is None:
            continue

        if isinstance(after_1, int) and isinstance(after_2, int):
            assert after_2 >= after_1
            continue

        if isinstance(after_1, dict) and isinstance(after_2, dict):
            continue

        try:
            assert after_2 >= after_1
        except TypeError:
            pass


def _sources(data):
    return [x.get("src") for x in data]


def _ids(data):
    return [x.get("id") for x in data]


def _assert_no_dupes_in_page(data):
    ids = _ids(data)
    assert len(ids) == len(set(ids))


def _assert_pages_no_overlap(res_1, res_2):
    assert not (set(_ids(res_1.data)) & set(_ids(res_2.data)))


def _inner_append_config(*, merger_id: str, subfeed_id: str, method_name: str, dedup_priority: int):
    return {
        "merger_id": merger_id,
        "type": "merger_append",
        # Important: dedup deletion priority must be visible at this node so parent mergers
        # can fetch higher-priority subtrees first when a dedup wrapper is active.
        "dedup_priority": dedup_priority,
        "shuffle": False,
        "items": [
            {
                "subfeed_id": subfeed_id,
                "type": "subfeed",
                "method_name": method_name,
                "dedup_priority": dedup_priority,
            }
        ],
    }


def _build_deep_priority_tree_for_merger_type(*, merger_type: str):
    """Return a deep tree config where low/high leaves overlap by id.

    The inner leaves are wrapped into an append merger to ensure a "deep" tree even
    when the outer merger is flat.
    """

    low = _inner_append_config(merger_id="inner_low", subfeed_id="sf_low", method_name="low", dedup_priority=0)
    high = _inner_append_config(merger_id="inner_high", subfeed_id="sf_high", method_name="high", dedup_priority=100)

    if merger_type == "merger_append":
        return {
            "merger_id": "outer_append",
            "type": "merger_append",
            "shuffle": False,
            # Put low first intentionally; priority must still make high win for overlapping ids.
            "items": [low, high],
        }

    if merger_type == "merger_distribute":
        return {
            "merger_id": "outer_dist",
            "type": "merger_distribute",
            "distribution_key": "user_id",
            # Put low first intentionally.
            "items": [low, high],
        }

    if merger_type == "merger_percentage":
        return {
            "merger_id": "outer_pct",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {"percentage": 50, "data": low},
                {"percentage": 50, "data": high},
            ],
        }

    if merger_type == "merger_percentage_gradient":
        return {
            "merger_id": "outer_grad",
            "type": "merger_percentage_gradient",
            "item_from": {"percentage": 60, "data": low},
            "item_to": {"percentage": 40, "data": high},
            "step": 20,
            "size_to_step": 5,
            "shuffle": False,
        }

    if merger_type == "merger_positional":
        # High priority on positional branch so it must win duplicates.
        high_pos = _inner_append_config(
            merger_id="inner_pos_high",
            subfeed_id="sf_high",
            method_name="high",
            dedup_priority=100,
        )
        low_def = _inner_append_config(
            merger_id="inner_def_low",
            subfeed_id="sf_low",
            method_name="low",
            dedup_priority=0,
        )
        return {
            "merger_id": "outer_pos",
            "type": "merger_positional",
            "positions": [1, 3, 5, 7, 9, 11],
            "positional": high_pos,
            "default": low_def,
        }

    raise AssertionError(f"Unknown merger_type: {merger_type}")


@pytest.mark.asyncio
async def test_dedup_positional_slot_ownership_cursor_backend() -> None:
    """Positional slots must remain owned by the positional branch.

    Deduplication must not drop items *after* the positional merge (which would shift indices).
    Instead, duplicates must be skipped inside the leaf source that owns the slot.
    """

    # Default branch has early ids 1..3, which will be seen first.
    default_items = [{"id": i, "src": "default"} for i in range(1, 300)]

    # Positional branch starts with duplicates 1..3; it must skip them and fetch 4.. instead.
    positional_items = [{"id": i, "src": "pos"} for i in range(1, 300)]

    methods_dict = {
        "default": make_offset_paged_method(default_items),
        "pos": make_offset_paged_method(positional_items),
    }

    config = {
        "merger_id": "dedup_wrapper",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "max_refill_loops": 20,
        "data": {
            "merger_id": "positional_mix",
            "type": "merger_positional",
            # Ensure positional inserts exist on both pages for limit=6:
            # page1 uses (1,3,5), page2 uses (7,9,11) which map to the same in-page slots.
            "positions": [1, 3, 5, 7, 9, 11],
            "positional": {"subfeed_id": "sf_pos", "type": "subfeed", "method_name": "pos"},
            "default": {"subfeed_id": "sf_default", "type": "subfeed", "method_name": "default"},
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=6,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res_1.data) == 6
    _assert_no_dupes_in_page(res_1.data)

    # Slot ownership: configured positions [1,3,5] are the positional branch.
    assert _sources(res_1.data)[0] == "pos"
    assert _sources(res_1.data)[2] == "pos"
    assert _sources(res_1.data)[4] == "pos"

    # Next page: still no overlap across pages, and positional slots remain owned.
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=6,
        next_page=res_1.next_page,
    )

    assert len(res_2.data) == 6
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    assert _sources(res_2.data)[0] == "pos"
    assert _sources(res_2.data)[2] == "pos"
    assert _sources(res_2.data)[4] == "pos"

    _assert_cursor_monotonic_if_present(res_1, res_2, keys=["sf_pos", "sf_default", "positional_mix", "dedup_wrapper"])


@pytest.mark.asyncio
async def test_dedup_percentage_slot_ownership_cursor_backend() -> None:
    """Percentage mixing order must be preserved even with duplicates across sources."""

    # A is called first by the percentage merger; its ids will be seen before B.
    a_items = [{"id": i, "src": "A"} for i in range(1, 300)]

    # B starts with duplicates 1..3; it must skip them and fetch unique tail items.
    # Same IDs as A to force cross-source duplicates.
    b_items = [{"id": i, "src": "B"} for i in range(1, 300)]

    methods_dict = {
        "a": make_offset_paged_method(a_items),
        "b": make_offset_paged_method(b_items),
    }

    config = {
        "merger_id": "dedup_wrapper_pct",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "pct_mix",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {"percentage": 50, "data": {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"}},
                {"percentage": 50, "data": {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"}},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res_1.data) == 10
    _assert_no_dupes_in_page(res_1.data)

    # Slot ownership: percentage merge alternates when list sizes are equal.
    sources_1 = _sources(res_1.data)
    assert sources_1[0] == "A"
    assert sources_1[1] == "B"
    assert sources_1[2] == "A"
    assert sources_1[3] == "B"

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=res_1.next_page,
    )

    assert len(res_2.data) == 10
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    sources_2 = _sources(res_2.data)
    assert sources_2[0] == "A"
    assert sources_2[1] == "B"

    _assert_cursor_monotonic_if_present(res_1, res_2, keys=["sf_a", "sf_b", "pct_mix", "dedup_wrapper_pct"])


@pytest.mark.asyncio
async def test_dedup_deep_tree_cursor_backend() -> None:
    """Dedup must work through deep merger trees (wrapping leaf methods)."""

    # Leaf sources: intentionally overlapping ids across different leaves.
    p_items = [{"id": i, "src": "P"} for i in range(1, 30)]
    d1_items = [{"id": i, "src": "D1"} for i in range(1, 30)]  # overlaps P
    d2_items = [{"id": 100 + i, "src": "D2"} for i in range(1, 30)]

    methods_dict = {
        "p": make_offset_paged_method(p_items),
        "d1": make_offset_paged_method(d1_items),
        "d2": make_offset_paged_method(d2_items),
    }

    # Deep tree: Dedup -> Positional(default=Percentage(D1,D2), positional=SubFeed(P))
    config = {
        "merger_id": "dedup_deep",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "pos_deep",
            "type": "merger_positional",
            # Ensure positional positions exist on both page 1 (1,4) and page 2 (9,12) for limit=8.
            "positions": [1, 4, 9, 12],
            "positional": {"subfeed_id": "sf_p", "type": "subfeed", "method_name": "p"},
            "default": {
                "merger_id": "pct_deep",
                "type": "merger_percentage",
                "shuffle": False,
                "items": [
                    {"percentage": 50, "data": {"subfeed_id": "sf_d1", "type": "subfeed", "method_name": "d1"}},
                    {"percentage": 50, "data": {"subfeed_id": "sf_d2", "type": "subfeed", "method_name": "d2"}},
                ],
            },
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=8,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res_1.data) == 8
    _assert_no_dupes_in_page(res_1.data)

    # Positional ownership must hold even with deep defaults.
    assert _sources(res_1.data)[0] == "P"  # position 1
    assert _sources(res_1.data)[3] == "P"  # position 4

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=8,
        next_page=res_1.next_page,
    )

    assert len(res_2.data) == 8
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    assert _sources(res_2.data)[0] == "P"
    assert _sources(res_2.data)[3] == "P"


@pytest.mark.parametrize(
    "merger_type",
    [
        "merger_append",
        "merger_distribute",
        "merger_positional",
        "merger_percentage",
        "merger_percentage_gradient",
    ],
)
@pytest.mark.asyncio
async def test_dedup_deletion_priority_works_for_deep_trees_all_merger_types(merger_type: str) -> None:
    """Deletion priority must work even in deep trees, across merger types.

    For overlapping ids, higher dedup_priority leaf must supply the winning entity.
    """

    # For mixing mergers (percentage/gradient/positional), identical id ranges are enough: the
    # high-priority leaf should claim the first chunk of ids and the other leaf must skip them.
    #
    # For append/distribute, we must ensure BOTH branches contribute to the output (otherwise
    # "priority" is unobservable because earlier branches can fill the page). We do that by
    # making the low branch short and duplicate-heavy.
    if merger_type in {"merger_append", "merger_distribute"}:
        low_items = [
            {"id": 1, "user_id": "u0", "src": "low"},
            {"id": 2, "user_id": "u1", "src": "low"},
            {"id": 3, "user_id": "u2", "src": "low"},
            {"id": 1000, "user_id": "u0", "src": "low"},
            {"id": 1001, "user_id": "u1", "src": "low"},
        ]
        high_items = [{"id": i, "user_id": f"u{i%3}", "src": "high"} for i in range(1, 200)]
    else:
        low_items = [{"id": i, "user_id": f"u{i%3}", "src": "low"} for i in range(1, 200)]
        high_items = [{"id": i, "user_id": f"u{i%3}", "src": "high"} for i in range(1, 200)]

    methods_dict = {
        "low": make_offset_paged_method(low_items),
        "high": make_offset_paged_method(high_items),
    }

    deep_tree = _build_deep_priority_tree_for_merger_type(merger_type=merger_type)
    config = {
        "merger_id": f"dedup_priority_deep_{merger_type}",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": deep_tree,
    }

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    _assert_no_dupes_in_page(res.data)

    # Priority is about which source wins overlapping ids (not about output order).
    winning = {item["id"]: item["src"] for item in res.data}
    assert all(winning[i] == "high" for i in range(1, 6) if i in winning)

    # Placement invariant for positional: positional slots must still be owned by positional branch.
    if merger_type == "merger_positional":
        sources = _sources(res.data)
        assert sources[0] == "high"
        assert sources[2] == "high"
        assert sources[4] == "high"


@pytest.mark.asyncio
async def test_dedup_overfetch_factor_does_not_skip_unseen_items_in_deep_tree_cursors() -> None:
    """When overfetch_factor>1, leaf cursors must be rewound to inspected count.

    This is a regression test for the "safe overfetch" logic: we may request more
    than we need from a leaf source, but we must not advance that leaf cursor past
    un-inspected items. In a deep tree, this must hold for all descendant SubFeeds.
    """

    p_items = [{"id": 1000 + i, "src": "P"} for i in range(1, 200)]
    d1_items = [{"id": i, "src": "D1"} for i in range(1, 200)]
    d2_items = [{"id": 500 + i, "src": "D2"} for i in range(1, 200)]

    methods_dict = {
        "p": make_offset_paged_method(p_items),
        "d1": make_offset_paged_method(d1_items),
        "d2": make_offset_paged_method(d2_items),
    }

    config = {
        "merger_id": "dedup_overfetch",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "overfetch_factor": 3,
        "data": {
            "merger_id": "pos_overfetch",
            "type": "merger_positional",
            "positions": [1, 4, 9, 12],
            "positional": {"subfeed_id": "sf_p", "type": "subfeed", "method_name": "p"},
            "default": {
                "merger_id": "pct_overfetch",
                "type": "merger_percentage",
                "shuffle": False,
                "items": [
                    {"percentage": 50, "data": {"subfeed_id": "sf_d1", "type": "subfeed", "method_name": "d1"}},
                    {"percentage": 50, "data": {"subfeed_id": "sf_d2", "type": "subfeed", "method_name": "d2"}},
                ],
            },
        },
    }

    merger = parse_model(MergerDeduplication, config)

    # Page 1
    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=8,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res_1.data) == 8
    _assert_no_dupes_in_page(res_1.data)

    # Dedup merger cursor must exist and advance page.
    assert "dedup_overfetch" in res_1.next_page.data
    assert res_1.next_page.data["dedup_overfetch"].page == 2
    assert res_1.next_page.data["dedup_overfetch"].after is not None

    # Positional merger cursor must exist and advance page.
    assert "pos_overfetch" in res_1.next_page.data
    assert res_1.next_page.data["pos_overfetch"].page == 2

    # Deep descendant cursors: positional leaf requests 2 items; percentage leaves request 4 each.
    # With overfetch_factor=3, internal calls may request 6/12, but cursor must not advance that far.
    assert res_1.next_page.data["sf_p"].after == 2
    assert res_1.next_page.data["sf_d1"].after == 4
    assert res_1.next_page.data["sf_d2"].after == 4

    # Page 2 (monotonic advancement, still no over-advancement)
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=8,
        next_page=res_1.next_page,
    )

    assert len(res_2.data) == 8
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    assert res_2.next_page.data["dedup_overfetch"].page == 3
    assert res_2.next_page.data["pos_overfetch"].page == 3

    assert res_2.next_page.data["sf_p"].after == 4
    assert res_2.next_page.data["sf_d1"].after == 8
    assert res_2.next_page.data["sf_d2"].after == 8

    _assert_cursor_monotonic_if_present(
        res_1,
        res_2,
        keys=["sf_p", "sf_d1", "sf_d2", "pos_overfetch", "dedup_overfetch"],
    )


@pytest.mark.asyncio
async def test_dedup_page_zero_resets_seen_and_descendant_cursors() -> None:
    items = [{"id": i, "src": "S"} for i in range(1, 50)]
    methods_dict = {"s": make_offset_paged_method(items)}

    config = {
        "merger_id": "dedup_reset",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {"subfeed_id": "sf_stream", "type": "subfeed", "method_name": "s"},
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )
    assert _ids(res_1.data) == [1, 2, 3, 4, 5]

    # Simulate client "full reload": page=0 for the dedup merger.
    # Also include the stale descendant cursor; dedup should clear it.
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(
            data={
                "dedup_reset": FeedResultNextPageInside(page=0, after=res_1.next_page.data["dedup_reset"].after),
                "sf_stream": res_1.next_page.data["sf_stream"],
            }
        ),
    )

    # Must restart from the beginning.
    assert _ids(res_2.data) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_redis_backend_cross_page(redis_client) -> None:
    items_a = [{"id": i, "src": "A"} for i in range(1, 300)]
    # Same IDs as A to force cross-source duplicates.
    items_b = [{"id": i, "src": "B"} for i in range(1, 300)]

    methods_dict = {
        "a": make_offset_paged_method(items_a),
        "b": make_offset_paged_method(items_b),
    }

    config = {
        "merger_id": "dedup_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "data": {
            "merger_id": "pct_mix",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {"percentage": 50, "data": {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"}},
                {"percentage": 50, "data": {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"}},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client,
        custom_deduplication_key="t1",
    )

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=res_1.next_page,
        redis_client=redis_client,
        custom_deduplication_key="t1",
    )

    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    # Redis backend should not store seen ids in cursor after.
    assert "dedup_redis" in res_2.next_page.data
    assert res_2.next_page.data["dedup_redis"].after is None

    # Ensure state is persisted in Redis.
    key = "dedup:dedup_redis:u:t1"
    members = redis_client.zrange(key, 0, -1)
    if inspect.iscoroutine(members):
        members = await members
    assert len(members) >= len(set(_ids(res_1.data) + _ids(res_2.data)))


@pytest.mark.asyncio
async def test_dedup_append_cursor_backend_across_pages_and_refill_advances_leaf_cursor_exactly() -> None:
    """Append: across pages there is no overlap; refill advances cursors correctly.

    This uses a max_per_call=1 method for the duplicate-heavy leaf so the wrapper
    must do multiple client calls (refill loops).
    """

    a_items = [{"id": 1, "src": "A"}, {"id": 2, "src": "A"}]
    b_items = [{"id": i, "src": "B"} for i in range(1, 50)]

    methods_dict = {
        "a": make_offset_paged_method(a_items),
        # Force multiple internal calls.
        "b": make_offset_paged_method(b_items, max_per_call=1),
    }

    config = {
        "merger_id": "dedup_append_pages",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "max_refill_loops": 50,
        "data": {
            "merger_id": "append_mix",
            "type": "merger_append",
            "shuffle": False,
            "items": [
                {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
                {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    assert _ids(res_1.data) == [1, 2, 3, 4, 5]
    assert res_1.next_page.data["dedup_append_pages"].page == 2

    # In dedup-active append mode, each child is requested with the full page limit (5).
    # B must therefore collect 5 unique items while skipping 2 duplicates -> scan ids 1..7.
    assert res_1.next_page.data["sf_b"].after == 7

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=res_1.next_page,
    )

    assert len(res_2.data) == 5
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)


@pytest.mark.asyncio
async def test_dedup_distribute_cursor_backend_across_pages_preserves_source_refill() -> None:
    """Distribute: duplicates skipped per-leaf and page slices don't overlap."""

    # A is short so B must contribute.
    items_a = [{"id": i, "user_id": f"u{i%2}", "src": "A"} for i in range(1, 4)]
    # B overlaps A by id and continues.
    items_b = [{"id": i, "user_id": f"u{i%2}", "src": "B"} for i in range(1, 200)]

    methods_dict = {
        "a": make_offset_paged_method(items_a),
        "b": make_offset_paged_method(items_b),
    }

    config = {
        "merger_id": "dedup_dist_pages",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "dist",
            "type": "merger_distribute",
            "distribution_key": "user_id",
            "items": [
                {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
                {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res_1 = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=10, next_page=FeedResultNextPage(data={}))
    res_2 = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=10, next_page=res_1.next_page)

    assert len(res_1.data) == 10
    assert len(res_2.data) == 10
    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    # Placement/refill: B must skip duplicate ids 1..3 and still fill the page.
    b_ids_1 = [x["id"] for x in res_1.data if x.get("src") == "B"]
    assert b_ids_1 and min(b_ids_1) >= 4


@pytest.mark.asyncio
async def test_dedup_percentage_gradient_cursor_backend_across_pages() -> None:
    a_items = [{"id": i, "src": "A"} for i in range(1, 300)]
    b_items = ([{"id": i, "src": "B"} for i in range(1, 30)] + [{"id": 1000 + i, "src": "B"} for i in range(1, 300)])

    methods_dict = {
        "a": make_offset_paged_method(a_items),
        "b": make_offset_paged_method(b_items),
    }

    config = {
        "merger_id": "dedup_grad_pages",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "max_refill_loops": 50,
        "data": {
            "merger_id": "grad_mix",
            "type": "merger_percentage_gradient",
            "item_from": {"percentage": 60, "data": {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"}},
            "item_to": {"percentage": 40, "data": {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"}},
            "step": 20,
            "size_to_step": 5,
            "shuffle": False,
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res_1 = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=10, next_page=FeedResultNextPage(data={}))
    res_2 = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=10, next_page=res_1.next_page)

    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    # Gradient merger cursor should exist and advance.
    assert res_1.next_page.data["grad_mix"].page == 2
    assert res_2.next_page.data["grad_mix"].page == 3


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_redis_backend_cross_page_append(redis_client) -> None:
    items_a = [{"id": i, "src": "A"} for i in range(1, 20)]
    items_b = [{"id": i, "src": "B"} for i in range(1, 300)]

    methods_dict = {
        "a": make_offset_paged_method(items_a),
        "b": make_offset_paged_method(items_b),
    }

    config = {
        "merger_id": "dedup_redis_append",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "data": {
            "merger_id": "append_mix",
            "type": "merger_append",
            "shuffle": False,
            "items": [
                {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
                {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client,
        custom_deduplication_key="t2",
    )
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=res_1.next_page,
        redis_client=redis_client,
        custom_deduplication_key="t2",
    )

    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)
    assert res_2.next_page.data["dedup_redis_append"].after is None


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_wrapper_with_view_session_merger(redis_client) -> None:
    """Dedup wrapper must work when the child is a view_session merger."""

    # Two leaves with overlapping ids; view_session computes a session once.
    items_low = [{"id": i, "src": "low"} for i in range(1, 100)]
    items_high = [{"id": i, "src": "high"} for i in range(1, 100)]

    methods_dict = {
        "low": make_offset_paged_method(items_low),
        "high": make_offset_paged_method(items_high),
    }

    config = {
        "merger_id": "dedup_vs",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "vs",
            "type": "merger_view_session",
            "session_size": 30,
            "session_live_time": 60,
            "deduplicate": False,
            "shuffle": False,
            "data": {
                "merger_id": "pct",
                "type": "merger_percentage",
                "shuffle": False,
                "items": [
                    {"percentage": 50, "data": {"subfeed_id": "sf_low", "type": "subfeed", "method_name": "low", "dedup_priority": 0}},
                    {"percentage": 50, "data": {"subfeed_id": "sf_high", "type": "subfeed", "method_name": "high", "dedup_priority": 100}},
                ],
            },
        },
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client,
        custom_view_session_key="vs1",
    )

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=res_1.next_page,
        redis_client=redis_client,
        custom_view_session_key="vs1",
    )

    _assert_no_dupes_in_page(res_1.data)
    _assert_no_dupes_in_page(res_2.data)
    _assert_pages_no_overlap(res_1, res_2)

    # Deletion priority: for the overlapping early ids, the winning entity must be from high.
    winning = {item["id"]: item["src"] for item in (res_1.data + res_2.data)}
    assert all(winning[i] == "high" for i in range(1, 11) if i in winning)


@pytest.mark.asyncio
async def test_dedup_append_distribute_cursor_backend_no_dupes() -> None:
    items_a = [{"id": i, "user_id": f"u{i%3}", "src": "A"} for i in range(1, 200)]
    items_b = [{"id": i, "user_id": f"u{i%3}", "src": "B"} for i in range(1, 200)]

    methods_dict = {
        "a": make_offset_paged_method(items_a),
        "b": make_offset_paged_method(items_b),
    }

    config = {
        "merger_id": "dedup_dist",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "dist",
            "type": "merger_distribute",
            "distribution_key": "user_id",
            "items": [
                {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
                {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=30,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) == 30
    _assert_no_dupes_in_page(res.data)


@pytest.mark.asyncio
async def test_dedup_in_page_deletion_priority_keeps_high_priority_even_if_config_order_is_low_first() -> None:
    """High dedup_priority source must not be deleted even if called later in config order.

    We use a percentage merger where both branches have overlapping ids.
    The "high" branch is second in config, but has higher dedup_priority.
    """

    low_items = [{"id": i, "src": "low"} for i in range(1, 200)]
    high_items = [{"id": i, "src": "high"} for i in range(1, 200)]

    methods_dict = {
        "low": make_offset_paged_method(low_items),
        "high": make_offset_paged_method(high_items),
    }

    config = {
        "merger_id": "dedup_priority",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "data": {
            "merger_id": "pct",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {
                    "percentage": 50,
                    "data": {"subfeed_id": "sf_low", "type": "subfeed", "method_name": "low", "dedup_priority": 0},
                },
                {
                    "percentage": 50,
                    "data": {"subfeed_id": "sf_high", "type": "subfeed", "method_name": "high", "dedup_priority": 100},
                },
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    _assert_no_dupes_in_page(res.data)
    # Priority is about which source "wins" for a given dedup_key, not about output order.
    # With 50/50 limits, the high-priority branch should supply ids 1..5, while the low-priority
    # branch will be advanced to avoid duplicates.
    winning = {item["id"]: item["src"] for item in res.data}
    assert all(winning[i] == "high" for i in range(1, 6))


@pytest.mark.asyncio
async def test_dedup_percentage_gradient_slot_ownership_cursor_backend() -> None:
    """Dedup must preserve gradient chunking semantics.

    For limit=10, size_to_step=5, from/to percentages should yield chunks:
    - first 5: 3 from A, 2 from B
    - next 5: 2 from A, 3 from B
    Dedup must refill within each leaf so these chunk sizes remain true.
    """

    a_items = [{"id": i, "src": "A"} for i in range(1, 300)]
    # Start with duplicates, then provide unique tail.
    b_items = ([{"id": i, "src": "B"} for i in range(1, 30)] + [{"id": 1000 + i, "src": "B"} for i in range(1, 300)])

    methods_dict = {
        "a": make_offset_paged_method(a_items),
        "b": make_offset_paged_method(b_items),
    }

    config = {
        "merger_id": "dedup_gradient",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "max_refill_loops": 50,
        "data": {
            "merger_id": "grad_mix",
            "type": "merger_percentage_gradient",
            "item_from": {
                "percentage": 60,
                "data": {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
            },
            "item_to": {
                "percentage": 40,
                "data": {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            },
            "step": 20,
            "size_to_step": 5,
            "shuffle": False,
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) == 10
    _assert_no_dupes_in_page(res.data)

    sources = _sources(res.data)
    assert sources[:3] == ["A", "A", "A"]
    assert sources[3:5] == ["B", "B"]
    assert sources[5:7] == ["A", "A"]
    assert sources[7:10] == ["B", "B", "B"]


@pytest.mark.asyncio
async def test_dedup_preserves_append_priority_and_advances_cursors_cursor_backend() -> None:
    """Append order is the priority signal; dedup must not let later sources win duplicates.

    Also asserts that a leaf cursor advances even when items are skipped as duplicates.
    """

    a_items = [
        {"id": 1, "src": "A"},
        {"id": 2, "src": "A"},
    ]
    # B repeats A's ids first, then continues with unique ids.
    b_items = [{"id": i, "src": "B"} for i in range(1, 50)]

    methods_dict = {
        "a": make_offset_paged_method(a_items),
        "b": make_offset_paged_method(b_items),
    }

    config = {
        "merger_id": "dedup_append",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "max_refill_loops": 20,
        "data": {
            "merger_id": "append_mix",
            "type": "merger_append",
            "shuffle": False,
            "items": [
                {"subfeed_id": "sf_a", "type": "subfeed", "method_name": "a"},
                {"subfeed_id": "sf_b", "type": "subfeed", "method_name": "b"},
            ],
        },
    }

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    assert _ids(res.data) == [1, 2, 3, 4, 5]
    assert _sources(res.data)[:2] == ["A", "A"]
    assert _sources(res.data)[2:] == ["B", "B", "B"]

    # B had to scan past duplicated ids 1 and 2, so its cursor should advance
    # farther than the number of items it contributed to the final page.
    assert "sf_b" in res.next_page.data
    assert isinstance(res.next_page.data["sf_b"].after, int)
    b_contributed = sum(1 for x in res.data if x.get("src") == "B")
    assert res.next_page.data["sf_b"].after > b_contributed
