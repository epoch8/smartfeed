import asyncio
import inspect

import pytest

from smartfeed.schemas import FeedResultClient, FeedResultNextPage, FeedResultNextPageInside, MergerDeduplication
from tests.fixtures import dedup_helpers as dh
from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


PROFILES_B_1_TO_8 = {
    "p0": [{"id": 1, "src": "B"}, {"id": 3, "src": "B"}, {"id": 5, "src": "B"}, {"id": 7, "src": "B"}],
    "p1": [{"id": 2, "src": "B"}, {"id": 4, "src": "B"}, {"id": 6, "src": "B"}, {"id": 8, "src": "B"}],
}


def _assert_winning_src_for_ids(data, ids, expected_src: str) -> None:
    winning = {item["id"]: item["src"] for item in data}
    assert all(winning[i] == expected_src for i in ids if i in winning)


def _make_items_by_ids(src: str, ids, *, user_id_mod: int):
    return [{"id": i, "user_id": f"u{i % user_id_mod}", "src": src} for i in ids]


@pytest.mark.asyncio
async def test_dedup_positional_slot_ownership_cursor_backend() -> None:
    """Positional slots must remain owned by the positional branch.

    Deduplication must not drop items *after* the positional merge (which would shift indices).
    Instead, duplicates must be skipped inside the leaf source that owns the slot.
    """

    # Default branch has early ids 1..3, which will be seen first.
    default_items = dh.make_items("default", 1, 300)

    # Positional branch starts with duplicates 1..3; it must skip them and fetch 4.. instead.
    positional_items = dh.make_items("pos", 1, 300)

    methods_dict = {
        "default": dh.make_offset_paged_method(default_items),
        "pos": dh.make_offset_paged_method(positional_items),
    }

    config = dh._dedup_config(
        "dedup_wrapper",
        dh._positional_config(
            "positional_mix",
            # Ensure positional inserts exist on both pages for limit=6:
            # page1 uses (1,3,5), page2 uses (7,9,11) which map to the same in-page slots.
            positions=[1, 3, 5, 7, 9, 11],
            positional=dh._subfeed("sf_pos", "pos"),
            default=dh._subfeed("sf_default", "default"),
        ),
        max_refill_loops=20,
    )

    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 6)

    assert len(res_1.data) == 6
    dh._assert_no_dupes_in_page(res_1.data)

    # Slot ownership: configured positions [1,3,5] are the positional branch.
    dh._assert_sources_at_positions(res_1.data, [1, 3, 5], "pos")

    # Next page: still no overlap across pages, and positional slots remain owned.
    assert len(res_2.data) == 6
    dh._assert_two_pages_no_dupes(res_1, res_2)
    dh._assert_sources_at_positions(res_2.data, [1, 3, 5], "pos")

    dh._assert_cursor_monotonic_if_present(res_1, res_2, keys=["sf_pos", "sf_default", "positional_mix", "dedup_wrapper"])


@pytest.mark.asyncio
async def test_dedup_percentage_slot_ownership_cursor_backend() -> None:
    """Percentage mixing order must be preserved even with duplicates across sources."""

    # A is called first by the percentage merger; its ids will be seen before B.
    a_items = dh.make_items("A", 1, 300)

    # B starts with duplicates 1..3; it must skip them and fetch unique tail items.
    # Same IDs as A to force cross-source duplicates.
    b_items = dh.make_items("B", 1, 300)

    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=a_items,
        items_b=b_items,
        merger_id="dedup_wrapper_pct",
        child_builder=lambda sf_a, sf_b: dh._percentage_config(
            "pct_mix",
            items=dh._percentage_items(sf_a, sf_b),
        ),
    )
    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 10)

    assert len(res_1.data) == 10
    dh._assert_no_dupes_in_page(res_1.data)

    # Slot ownership: percentage merge alternates when list sizes are equal.
    assert dh._sources(res_1.data)[:4] == ["A", "B", "A", "B"]

    assert len(res_2.data) == 10
    dh._assert_two_pages_no_dupes(res_1, res_2)

    assert dh._sources(res_2.data)[:2] == ["A", "B"]

    dh._assert_cursor_monotonic_if_present(res_1, res_2, keys=["sf_a", "sf_b", "pct_mix", "dedup_wrapper_pct"])


@pytest.mark.asyncio
async def test_dedup_deep_tree_cursor_backend() -> None:
    """Dedup must work through deep merger trees (wrapping leaf methods)."""

    # Leaf sources: intentionally overlapping ids across different leaves.
    p_items = dh.make_items("P", 1, 30)
    d1_items = dh.make_items("D1", 1, 30)  # overlaps P
    d2_items = dh.make_items("D2", 1, 30, id_offset=100)

    config, methods_dict = dh._build_deep_positional_pct_dedup_merger(
        items_p=p_items,
        items_d1=d1_items,
        items_d2=d2_items,
        dedup_merger_id="dedup_deep",
        pos_merger_id="pos_deep",
        pct_merger_id="pct_deep",
        # Ensure positional positions exist on both page 1 (1,4) and page 2 (9,12) for limit=8.
        positions=[1, 4, 9, 12],
    )

    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 8)

    assert len(res_1.data) == 8
    dh._assert_no_dupes_in_page(res_1.data)

    # Positional ownership must hold even with deep defaults.
    dh._assert_sources_at_positions(res_1.data, [1, 4], "P")

    assert len(res_2.data) == 8
    dh._assert_two_pages_no_dupes(res_1, res_2)

    dh._assert_sources_at_positions(res_2.data, [1, 4], "P")


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
        low_items = _make_items_by_ids("low", [1, 2, 3, 1000, 1001], user_id_mod=3)
        high_items = dh.make_items("high", 1, 200, user_id_mod=3)
    else:
        low_items = dh.make_items("low", 1, 200, user_id_mod=3)
        high_items = dh.make_items("high", 1, 200, user_id_mod=3)

    methods_dict = {
        "low": dh.make_offset_paged_method(low_items),
        "high": dh.make_offset_paged_method(high_items),
    }

    deep_tree = dh._build_deep_priority_tree_for_merger_type(merger_type=merger_type)
    config = dh._dedup_config(f"dedup_priority_deep_{merger_type}", deep_tree)

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    dh._assert_no_dupes_in_page(res.data)

    # For append/distribute, priority is only observable if both branches contribute something.
    if merger_type in {"merger_append", "merger_distribute"}:
        sources = set(dh._sources(res.data))
        assert "high" in sources
        assert "low" in sources

    # Priority is about which source wins overlapping ids (not about output order).
    _assert_winning_src_for_ids(res.data, range(1, 6), "high")

    # Placement invariant for positional: positional slots must still be owned by positional branch.
    if merger_type == "merger_positional":
        sources = dh._sources(res.data)
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

    p_items = dh.make_items("P", 1, 200, id_offset=1000)
    d1_items = dh.make_items("D1", 1, 200)
    d2_items = dh.make_items("D2", 1, 200, id_offset=500)

    config, methods_dict = dh._build_deep_positional_pct_dedup_merger(
        items_p=p_items,
        items_d1=d1_items,
        items_d2=d2_items,
        dedup_merger_id="dedup_overfetch",
        pos_merger_id="pos_overfetch",
        pct_merger_id="pct_overfetch",
        positions=[1, 4, 9, 12],
        overfetch_factor=3,
    )

    merger = parse_model(MergerDeduplication, config)

    # Page 1/2
    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 8)

    assert len(res_1.data) == 8
    dh._assert_no_dupes_in_page(res_1.data)

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
    assert len(res_2.data) == 8
    dh._assert_two_pages_no_dupes(res_1, res_2)

    assert res_2.next_page.data["dedup_overfetch"].page == 3
    assert res_2.next_page.data["pos_overfetch"].page == 3

    assert res_2.next_page.data["sf_p"].after == 4
    assert res_2.next_page.data["sf_d1"].after == 8
    assert res_2.next_page.data["sf_d2"].after == 8

    dh._assert_cursor_monotonic_if_present(
        res_1,
        res_2,
        keys=["sf_p", "sf_d1", "sf_d2", "pos_overfetch", "dedup_overfetch"],
    )


@pytest.mark.asyncio
async def test_dedup_page_zero_resets_seen_and_descendant_cursors() -> None:
    items = dh.make_items("S", 1, 50)
    methods_dict = {"s": dh.make_offset_paged_method(items)}

    config = dh._dedup_config("dedup_reset", dh._subfeed("sf_stream", "s"))

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )
    assert dh._ids(res_1.data) == [1, 2, 3, 4, 5]

    # Simulate client "full reload": page=0 for the dedup merger.
    # Also include the stale descendant cursor; dedup should clear it.
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(
            data={
                "dedup_reset": FeedResultNextPageInside(page=0, after=res_1.next_page.data["dedup_reset"].after),
                # Use a deliberately bogus descendant cursor; the dedup wrapper must ignore/reset it.
                "sf_stream": FeedResultNextPageInside(page=99, after=999),
            }
        ),
    )

    # Must restart from the beginning.
    assert dh._ids(res_2.data) == [1, 2, 3, 4, 5]
    # And must not propagate the bogus descendant cursor.
    assert res_2.next_page.data["sf_stream"].after == 5
    assert res_2.next_page.data["sf_stream"].page == 2

@pytest.mark.asyncio
async def test_dedup_append_cursor_backend_across_pages_and_refill_advances_leaf_cursor_exactly() -> None:
    """Append: across pages there is no overlap; refill advances cursors correctly.

    This uses a max_per_call=1 method for the duplicate-heavy leaf so the wrapper
    must do multiple client calls (refill loops).
    """

    a_items = [{"id": 1, "src": "A"}, {"id": 2, "src": "A"}]
    b_items = dh.make_items("B", 1, 50)

    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=a_items,
        items_b=b_items,
        merger_id="dedup_append_pages",
        child_builder=lambda sf_a, sf_b: dh._append_config("append_mix", [sf_a, sf_b]),
        spec_b=dh._two_subfeed_spec(name="b", subfeed_id="sf_b", max_per_call=1),
        dedup_kwargs={"max_refill_loops": 50},
    )
    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 5)

    assert dh._ids(res_1.data) == [1, 2, 3, 4, 5]
    assert dh._sources(res_1.data) == ["A", "A", "B", "B", "B"]

    assert res_1.next_page.data["dedup_append_pages"].page == 2

    # In default arbitrate mode, B only needs to scan far enough to fill the remaining
    # portion of the page after arbitration (here: 3 items: ids 3..5).
    assert res_1.next_page.data["sf_b"].after == 5
    b_contributed = sum(1 for x in res_1.data if x.get("src") == "B")
    assert res_1.next_page.data["sf_b"].after > b_contributed

    # A is exhausted after 2 reads; ensure cursor reflects that.
    assert res_1.next_page.data["sf_a"].after == 2

    assert len(res_2.data) == 5
    dh._assert_two_pages_no_dupes(res_1, res_2)
    # Across two pages, B should have advanced exactly 5 more items.
    assert res_2.next_page.data["sf_b"].after == 10


@pytest.mark.asyncio
async def test_dedup_arbitrate_mode_runs_parallel_prefetch_and_arbitrates_winners() -> None:
    started_a = asyncio.Event()
    started_b = asyncio.Event()
    release = asyncio.Event()

    items_a = dh.make_items("A", 1, 200)
    items_b = dh.make_items("B", 1, 200)

    def make_method(*, items, started_event):
        async def _method(user_id, limit, next_page, **kwargs):  # pylint: disable=unused-argument
            started_event.set()
            await release.wait()
            offset = int(next_page.after or 0)
            data = items[offset : offset + limit]
            next_page.after = offset + len(data)
            next_page.page += 1
            return FeedResultClient(data=data, next_page=next_page, has_next_page=True)

        return _method

    methods_dict = {
        "a": make_method(items=items_a, started_event=started_a),
        "b": make_method(items=items_b, started_event=started_b),
    }

    config = dh._dedup_config(
        "dedup_arbitrate",
        dh._percentage_config(
            "pct",
            items=dh._percentage_items(dh._subfeed("sf_a", "a"), dh._subfeed("sf_b", "b")),
        ),
    )

    merger = parse_model(MergerDeduplication, config)

    task = asyncio.create_task(
        merger.get_data(methods_dict=methods_dict, user_id="u", limit=10, next_page=FeedResultNextPage(data={}))
    )

    # If execution is sequential, one of these would time out.
    await asyncio.wait_for(started_a.wait(), timeout=1)
    await asyncio.wait_for(started_b.wait(), timeout=1)
    release.set()

    res = await asyncio.wait_for(task, timeout=2)

    assert len(res.data) == 10
    dh._assert_no_dupes_in_page(res.data)
    # With equal priorities, stable tie-breaker should pick A (first branch) for overlapping keys.
    _assert_winning_src_for_ids(res.data, range(1, 6), "A")


@pytest.mark.asyncio
async def test_dedup_refill_loops_advance_dict_after_cursor_not_just_page() -> None:
    """Dedup refill loops must correctly advance dict-shaped `after` cursors."""

    # A produces ids 1,2.
    a_items = [{"id": 1, "src": "A"}, {"id": 2, "src": "A"}]

    # B produces ids 1.. in round-robin across profiles; cursor is per-profile offsets.
    b_profiles = PROFILES_B_1_TO_8

    methods_dict = {
        "a": dh.make_offset_paged_method(a_items),
        "b": dh.make_profile_dict_after_method(b_profiles),
    }

    # Use a percentage merger so B is asked for a small limit (2 items for limit=4).
    # This forces refill loops when B's first batch is all duplicates.
    config = dh._dedup_config(
        "dedup_dict_after",
        dh._percentage_config(
            "pct_mix",
            items=dh._percentage_items(
                dh._subfeed("sf_a", "a", dedup_priority=100),
                dh._subfeed("sf_b", "b", dedup_priority=0),
            ),
        ),
        max_refill_loops=50,
    )

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=4,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) == 4
    dh._assert_no_dupes_in_page(res.data)
    assert set(dh._ids(res.data)) == {1, 2, 3, 4}
    assert "sf_b" in res.next_page.data
    assert isinstance(res.next_page.data["sf_b"].after, dict)

    # B contributed 2 items (3,4) but must have *read* 4 items (1..4) to skip duplicates.
    b_after = res.next_page.data["sf_b"].after
    read_count = sum(int(v) for v in b_after.values())
    assert read_count == 4


@pytest.mark.asyncio
async def test_dedup_overfetch_does_not_overadvance_non_int_after_cursor() -> None:
    """overfetch_factor must not cause over-advancement for non-rewindable cursors."""

    # Single subfeed with dict after cursor; no dedup skips should happen.
    profiles = PROFILES_B_1_TO_8

    methods_dict = {
        "b": dh.make_profile_dict_after_method(profiles),
    }

    config = dh._dedup_config(
        "dedup_nonint_overfetch",
        dh._subfeed("sf_b", "b"),
        overfetch_factor=5,
    )

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(methods_dict=methods_dict, user_id="u", limit=4, next_page=FeedResultNextPage(data={}))

    assert len(res.data) == 4
    after = res.next_page.data["sf_b"].after
    assert isinstance(after, dict)
    # If overfetch were incorrectly applied, we'd see more than 4 reads.
    assert sum(int(v) for v in after.values()) == 4


@pytest.mark.asyncio
async def test_dedup_overfetch_rewinds_offset_cursor_when_first_batch_all_duplicates() -> None:
    """Overfetch should be safe: when we oversample, we must rewind offset cursors.

    Scenario:
    - A (high priority) returns ids 1..5
    - B (low priority) initially returns only duplicates (1..5)
    - On the next refill loop, B overfetches but must rewind `after` to inspected count
      so it doesn't skip items.
    """

    items_a = dh.make_items("A", 1, 300)
    items_b = dh.make_items("B", 1, 300)

    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=items_a,
        items_b=items_b,
        merger_id="dedup_overfetch_rewind",
        child_builder=lambda sf_a, sf_b: dh._percentage_config(
            "pct_mix",
            items=dh._percentage_items(sf_a, sf_b),
        ),
        spec_a=dh._two_subfeed_spec(dedup_priority=100),
        spec_b=dh._two_subfeed_spec(name="b", subfeed_id="sf_b", dedup_priority=0),
        dedup_kwargs={"overfetch_factor": 3, "max_refill_loops": 20},
    )
    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) == 10
    dh._assert_no_dupes_in_page(res.data)

    # A provides 1..5, B must provide 6..10.
    _assert_winning_src_for_ids(res.data, range(1, 6), "A")
    _assert_winning_src_for_ids(res.data, range(6, 11), "B")

    # Cursor rewind check:
    # - First loop for B reads 5 duplicates -> after becomes 5
    # - Second loop overfetches, but must rewind to inspected 5 more -> after should end at 10
    assert res.next_page.data["sf_b"].after == 10


@pytest.mark.parametrize(
    "items_a,items_b,min_b_id",
    [
        (dh.make_items("A", 1, 4, user_id_mod=2), dh.make_items("B", 1, 200, user_id_mod=2), 4),
        (dh.make_items("A", 1, 200, user_id_mod=3), dh.make_items("B", 1, 200, user_id_mod=3), None),
    ],
)
@pytest.mark.asyncio
async def test_dedup_distribute_cursor_backend_across_pages_preserves_source_refill(
    items_a, items_b, min_b_id
) -> None:
    """Distribute: duplicates skipped per-leaf and page slices don't overlap."""

    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=items_a,
        items_b=items_b,
        merger_id="dedup_dist_pages",
        child_builder=lambda sf_a, sf_b: dh._distribute_config("dist", [sf_a, sf_b]),
    )
    merger = parse_model(MergerDeduplication, config)
    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 10)

    assert len(res_1.data) == 10
    assert len(res_2.data) == 10
    dh._assert_two_pages_no_dupes(res_1, res_2)

    # Placement/refill: B must skip duplicate ids 1..3 and still fill the page.
    if min_b_id is not None:
        b_ids_1 = [x["id"] for x in res_1.data if x.get("src") == "B"]
        assert b_ids_1 and min(b_ids_1) >= min_b_id


@pytest.mark.asyncio
async def test_dedup_percentage_gradient_cursor_backend_across_pages() -> None:
    a_items = dh.make_items("A", 1, 300)
    b_items = dh.make_items("B", 1, 30) + dh.make_items("B", 1, 300, id_offset=1000)

    methods_dict = {
        "a": dh.make_offset_paged_method(a_items),
        "b": dh.make_offset_paged_method(b_items),
    }

    config = dh._dedup_config(
        "dedup_grad_pages",
        dh._gradient_config(
            "grad_mix",
            item_from={"percentage": 60, "data": dh._subfeed("sf_a", "a")},
            item_to={"percentage": 40, "data": dh._subfeed("sf_b", "b")},
            step=20,
            size_to_step=5,
            shuffle=False,
        ),
        max_refill_loops=50,
    )

    merger = parse_model(MergerDeduplication, config)
    res_1, res_2 = await dh._run_two_pages(merger, methods_dict, 10)

    dh._assert_two_pages_no_dupes(res_1, res_2)

    sources = dh._sources(res_1.data)
    assert sources == ["A", "A", "A", "B", "B", "A", "A", "B", "B", "B"]

    # Gradient merger cursor should exist and advance.
    assert res_1.next_page.data["grad_mix"].page == 2
    assert res_2.next_page.data["grad_mix"].page == 3


@pytest.mark.parametrize(
    "merger_id,custom_deduplication_key,items_a,items_b,child_builder",
    [
        (
            "dedup_redis",
            "t1",
            dh.make_items("A", 1, 300),
            dh.make_items("B", 1, 300),  # Same IDs as A to force cross-source duplicates.
            lambda sf_a, sf_b: dh._percentage_config("pct_mix", items=dh._percentage_items(sf_a, sf_b)),
        ),
        (
            "dedup_redis_append",
            "t2",
            dh.make_items("A", 1, 20),
            dh.make_items("B", 1, 300),
            lambda sf_a, sf_b: dh._append_config("append_mix", [sf_a, sf_b]),
        ),
    ],
)
@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_redis_backend_cross_page(
    redis_client,
    merger_id,
    custom_deduplication_key,
    items_a,
    items_b,
    child_builder,
) -> None:
    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=items_a,
        items_b=items_b,
        merger_id=merger_id,
        child_builder=child_builder,
        dedup_kwargs={"state_backend": "redis", "state_ttl_seconds": 60},
    )
    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(
        merger,
        methods_dict,
        10,
        redis_client=redis_client,
        custom_deduplication_key=custom_deduplication_key,
    )

    dh._assert_two_pages_no_dupes(res_1, res_2)

    # Redis backend should not store seen ids in cursor after.
    assert merger_id in res_2.next_page.data
    assert res_2.next_page.data[merger_id].after is None

    # Ensure state is persisted in Redis.
    key = f"dedup:{merger_id}:u:{custom_deduplication_key}"
    members = redis_client.zrange(key, 0, -1)
    if inspect.iscoroutine(members):
        members = await members
    assert len(members) >= len(set(dh._ids(res_1.data) + dh._ids(res_2.data)))


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_wrapper_with_view_session_merger(redis_client) -> None:
    """Dedup wrapper must work when the child is a view_session merger."""

    # Two leaves with overlapping ids; view_session computes a session once.
    items_low = dh.make_items("low", 1, 100)
    items_high = dh.make_items("high", 1, 100)

    methods_dict, subfeed_low, subfeed_high = dh._build_two_subfeed_methods(
        items_low,
        items_high,
        spec_a=dh._two_subfeed_spec(name="low", subfeed_id="sf_low", dedup_priority=0),
        spec_b=dh._two_subfeed_spec(name="high", subfeed_id="sf_high", dedup_priority=100),
    )

    config = dh._dedup_config(
        "dedup_vs",
        {
            "merger_id": "vs",
            "type": "merger_view_session",
            "session_size": 30,
            "session_live_time": 60,
            "deduplicate": False,
            "shuffle": False,
            "data": dh._percentage_config(
                "pct",
                items=dh._percentage_items(subfeed_low, subfeed_high),
            ),
        },
    )

    merger = parse_model(MergerDeduplication, config)

    res_1, res_2 = await dh._run_two_pages(
        merger,
        methods_dict,
        10,
        redis_client=redis_client,
        custom_view_session_key="vs1",
    )

    dh._assert_two_pages_no_dupes(res_1, res_2)

    # Deletion priority: for the overlapping early ids, the winning entity must be from high.
    _assert_winning_src_for_ids(res_1.data + res_2.data, range(1, 11), "high")


@pytest.mark.asyncio
async def test_dedup_in_page_deletion_priority_keeps_high_priority_even_if_config_order_is_low_first() -> None:
    """High dedup_priority source must not be deleted even if called later in config order.

    We use a percentage merger where both branches have overlapping ids.
    The "high" branch is second in config, but has higher dedup_priority.
    """

    low_items = dh.make_items("low", 1, 200)
    high_items = dh.make_items("high", 1, 200)

    config, methods_dict, _, _ = dh._build_two_subfeed_dedup_merger(
        items_a=low_items,
        items_b=high_items,
        merger_id="dedup_priority",
        child_builder=lambda sf_a, sf_b: dh._percentage_config(
            "pct",
            items=dh._percentage_items(sf_a, sf_b),
        ),
        spec_a=dh._two_subfeed_spec(name="low", subfeed_id="sf_low", dedup_priority=0),
        spec_b=dh._two_subfeed_spec(name="high", subfeed_id="sf_high", dedup_priority=100),
    )
    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=10,
        next_page=FeedResultNextPage(data={}),
    )

    dh._assert_no_dupes_in_page(res.data)
    # Priority is about which source "wins" for a given dedup_key, not about output order.
    # With 50/50 limits, the high-priority branch should supply ids 1..5, while the low-priority
    # branch will be advanced to avoid duplicates.
    _assert_winning_src_for_ids(res.data, range(1, 6), "high")

