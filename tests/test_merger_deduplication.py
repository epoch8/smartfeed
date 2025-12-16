import inspect

import pytest

from smartfeed.schemas import (
    MergerDeduplication,
    FeedResultClient,
    FeedResultNextPage,
    FeedResultNextPageInside,
)

from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


def make_offset_paged_method(items, *, max_per_call=None):
    async def _method(user_id, limit, next_page):  # pylint: disable=unused-argument
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


async def _run_two_pages(
    *,
    config,
    methods_dict,
    user_id,
    limit,
    redis_client_instance=None,
    **params,
):
    merger = parse_model(MergerDeduplication, config)
    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id=user_id,
        limit=limit,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client_instance,
        **params,
    )
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id=user_id,
        limit=limit,
        next_page=res_1.next_page,
        redis_client=redis_client_instance,
        **params,
    )
    return res_1, res_2


def _assert_dedup_backend_state(*, res, merger_id: str, state_backend: str) -> None:
    assert merger_id in res.next_page.data
    if state_backend == "cursor":
        assert isinstance(res.next_page.data[merger_id].after, dict)
    else:
        assert res.next_page.data[merger_id].after is None


def _ids(data):
    return [x["id"] for x in data]


def _assert_two_pages_no_overlap(res_1, res_2):
    ids_1 = set(_ids(res_1.data))
    ids_2 = set(_ids(res_2.data))
    assert len(ids_1) == len(res_1.data)
    assert len(ids_2) == len(res_2.data)
    assert not (ids_1 & ids_2)


def _assert_cursor_monotonic_if_present(res_1, res_2, keys):
    """Assert that cursor values monotonically advance for keys that are present.

    MergerDeduplication may stop early once it has enough unique items, so a
    descendant might not be called on a given page. This helper only asserts
    monotonicity when the cursor key exists in `res_1`.
    """

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

        # Merger cursors can be structured (dict), just require presence.
        if isinstance(after_1, dict) and isinstance(after_2, dict):
            continue

        # If values are comparable, enforce monotonicity; otherwise don't fail.
        try:
            assert after_2 >= after_1
        except TypeError:
            pass


@pytest.mark.asyncio
async def test_deduplication_merger_cursor_priority_and_cross_page() -> None:
    low_items = [
        {"id": 1, "src": "low"},
        {"id": 2, "src": "low"},
        {"id": 3, "src": "low"},
        {"id": 4, "src": "low"},
        {"id": 5, "src": "low"},
        # repeats later (cross-page duplicates)
        {"id": 3, "src": "low"},
        {"id": 4, "src": "low"},
        {"id": 6, "src": "low"},
        {"id": 7, "src": "low"},
        {"id": 8, "src": "low"},
        {"id": 9, "src": "low"},
        {"id": 10, "src": "low"},
    ]
    high_items = [
        {"id": 3, "src": "high"},
        {"id": 4, "src": "high"},
    ]

    methods_dict = {
        "low": make_offset_paged_method(low_items),
        "high": make_offset_paged_method(high_items),
    }

    config = {
        "merger_id": "dedup_example",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "overfetch_factor": 3,
        "items": [
            {
                "priority": 100,
                "data": {"subfeed_id": "sf_high", "type": "subfeed", "method_name": "high"},
            },
            {
                "priority": 0,
                "data": {"subfeed_id": "sf_low", "type": "subfeed", "method_name": "low"},
            },
        ],
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res_1.data) == 5
    ids_1 = [x["id"] for x in res_1.data]
    assert len(ids_1) == len(set(ids_1))
    # Priority: id 3 and 4 must come from high
    for x in res_1.data:
        if x["id"] in {3, 4}:
            assert x["src"] == "high"

    # Next page should not repeat 3/4 even though low repeats them later.
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=res_1.next_page,
    )

    ids_2 = [x["id"] for x in res_2.data]
    assert not (set(ids_1) & set(ids_2))

    # Ensure merger stores cursor state (compressed) in its own after.
    assert "dedup_example" in res_2.next_page.data
    assert isinstance(res_2.next_page.data["dedup_example"].after, dict)
    assert "z" in res_2.next_page.data["dedup_example"].after


@pytest.mark.asyncio
async def test_deduplication_merger_refill_to_limit() -> None:
    dup_items = [
        {"id": 1},
        {"id": 1},
        {"id": 1},
        {"id": 1},
        {"id": 1},
        {"id": 2},
        {"id": 3},
        {"id": 4},
        {"id": 5},
        {"id": 6},
    ]

    methods_dict = {
        "dups": make_offset_paged_method(dup_items),
    }

    config = {
        "merger_id": "dedup_refill",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "overfetch_factor": 4,
        "max_refill_loops": 10,
        "items": [
            {
                "priority": 0,
                "data": {"subfeed_id": "sf_dups", "type": "subfeed", "method_name": "dups"},
            }
        ],
    }

    merger = parse_model(MergerDeduplication, config)

    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )

    assert [x["id"] for x in res.data] == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_deduplication_merger_page_zero_resets_cursor_state() -> None:
    items = [{"id": i} for i in range(1, 50)]
    methods_dict = {"stream": make_offset_paged_method(items)}

    config = {
        "merger_id": "dedup_reset",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "overfetch_factor": 2,
        "items": [
            {
                "priority": 0,
                "data": {"subfeed_id": "sf_stream", "type": "subfeed", "method_name": "stream"},
            }
        ],
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )
    assert [x["id"] for x in res_1.data] == [1, 2, 3, 4, 5]

    # Simulate a full reload: page 0 requested again. Even if the client mistakenly
    # keeps the previous cursor payloads (including subfeed cursors), we start a new session.
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

    assert [x["id"] for x in res_2.data] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_redis_backend(redis_client) -> None:
    # This dataset repeats ids across pages (sliding window style)
    items = [
        {"id": 1},
        {"id": 2},
        {"id": 3},
        {"id": 2},
        {"id": 3},
        {"id": 4},
        {"id": 5},
        {"id": 6},
        {"id": 4},
        {"id": 7},
        {"id": 8},
    ]

    methods_dict = {"stream": make_offset_paged_method(items)}

    config = {
        "merger_id": "dedup_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 4,
        "items": [
            {
                "priority": 0,
                "data": {"subfeed_id": "sf_stream", "type": "subfeed", "method_name": "stream"},
            }
        ],
    }

    merger = parse_model(MergerDeduplication, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=4,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client,
        custom_deduplication_key="t1",
    )

    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=4,
        next_page=res_1.next_page,
        redis_client=redis_client,
        custom_deduplication_key="t1",
    )

    ids_1 = [x["id"] for x in res_1.data]
    ids_2 = [x["id"] for x in res_2.data]

    assert len(ids_1) == len(set(ids_1))
    assert len(ids_2) == len(set(ids_2))
    assert not (set(ids_1) & set(ids_2))

    # Redis backend should not store seen ids in cursor after.
    assert "dedup_redis" in res_2.next_page.data
    assert res_2.next_page.data["dedup_redis"].after is None

    # Ensure fixture works for both sync/async redis.
    key = "dedup:dedup_redis:u:t1"
    members = redis_client.smembers(key)
    if inspect.iscoroutine(members):
        members = await members
    assert len(members) >= len(set(ids_1 + ids_2))


@pytest.mark.asyncio
async def test_deduplication_merger_priority_replacement_across_loops_cursor_backend() -> None:
    # This test forces the higher-priority source to surface a duplicate only on a later call,
    # so we exercise the in-page replacement logic.
    # Important: dedup calls sources in descending priority. To ensure we exercise
    # replacement, we need a lower-priority source to introduce id=5 *before*
    # the high-priority source sees id=5 on a later refill loop.
    low_items = [
        {"id": 5, "src": "low"},
        {"id": 6, "src": "low"},
        {"id": 7, "src": "low"},
        {"id": 99, "src": "low"},
    ]
    mid_items = [
        {"id": 5, "src": "mid"},
        {"id": 98, "src": "mid"},
        {"id": 8, "src": "mid"},
        {"id": 9, "src": "mid"},
    ]
    high_items = [
        {"id": 1, "src": "high"},
        {"id": 5, "src": "high"},
        {"id": 2, "src": "high"},
        {"id": 3, "src": "high"},
    ]

    methods_dict = {
        "low": make_offset_paged_method(low_items, max_per_call=1),
        "mid": make_offset_paged_method(mid_items, max_per_call=1),
        "high": make_offset_paged_method(high_items, max_per_call=1),
    }

    config = {
        "merger_id": "dedup_priority_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "overfetch_factor": 1,
        "max_refill_loops": 10,
        "items": [
            {"priority": 100, "data": {"subfeed_id": "sf_high_p", "type": "subfeed", "method_name": "high"}},
            {"priority": 50, "data": {"subfeed_id": "sf_mid_p", "type": "subfeed", "method_name": "mid"}},
            {"priority": 0, "data": {"subfeed_id": "sf_low_p", "type": "subfeed", "method_name": "low"}},
        ],
    }

    res_1 = await parse_model(MergerDeduplication, config).get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=4,
        next_page=FeedResultNextPage(data={}),
    )

    # Ensure id=5 is present and comes from highest priority, even though low/mid can surface it earlier.
    winners = {x["id"]: x["src"] for x in res_1.data}
    assert winners[5] == "high"
    _assert_dedup_backend_state(res=res_1, merger_id="dedup_priority_cursor", state_backend="cursor")


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_priority_replacement_across_loops_redis_backend(redis_client) -> None:
    low_items = [
        {"id": 5, "src": "low"},
        {"id": 6, "src": "low"},
        {"id": 7, "src": "low"},
        {"id": 99, "src": "low"},
    ]
    mid_items = [
        {"id": 5, "src": "mid"},
        {"id": 98, "src": "mid"},
        {"id": 8, "src": "mid"},
        {"id": 9, "src": "mid"},
    ]
    high_items = [
        {"id": 1, "src": "high"},
        {"id": 5, "src": "high"},
        {"id": 2, "src": "high"},
        {"id": 3, "src": "high"},
    ]

    methods_dict = {
        "low": make_offset_paged_method(low_items, max_per_call=1),
        "mid": make_offset_paged_method(mid_items, max_per_call=1),
        "high": make_offset_paged_method(high_items, max_per_call=1),
    }

    config = {
        "merger_id": "dedup_priority_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 1,
        "max_refill_loops": 10,
        "items": [
            {"priority": 100, "data": {"subfeed_id": "sf_high_pr", "type": "subfeed", "method_name": "high"}},
            {"priority": 50, "data": {"subfeed_id": "sf_mid_pr", "type": "subfeed", "method_name": "mid"}},
            {"priority": 0, "data": {"subfeed_id": "sf_low_pr", "type": "subfeed", "method_name": "low"}},
        ],
    }

    res_1 = await parse_model(MergerDeduplication, config).get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=4,
        next_page=FeedResultNextPage(data={}),
        redis_client=redis_client,
        custom_deduplication_key="priority",
    )

    winners = {x["id"]: x["src"] for x in res_1.data}
    assert winners[5] == "high"
    _assert_dedup_backend_state(res=res_1, merger_id="dedup_priority_redis", state_backend="redis")


@pytest.mark.asyncio
async def test_deduplication_merger_with_append_and_three_sources_cursor_backend() -> None:
    # Inner MergerAppend (two subfeeds) + two extra subfeeds as separate dedup items.
    a_items = [{"id": i, "src": "a"} for i in range(1, 30)]
    b_items = [{"id": i, "src": "b"} for i in range(10, 40)]
    c_items = [{"id": i, "src": "c"} for i in range(20, 60)]
    d_items = [{"id": i, "src": "d"} for i in range(25, 70)]

    # Cap each subfeed to 1 item per call so dedup must invoke all children
    # (and therefore exercise nested cursor propagation).
    methods_dict = {
        "a": make_offset_paged_method(a_items, max_per_call=1),
        "b": make_offset_paged_method(b_items, max_per_call=1),
        "c": make_offset_paged_method(c_items, max_per_call=1),
        "d": make_offset_paged_method(d_items, max_per_call=1),
    }

    append_config = {
        "merger_id": "inner_append_unused",
        "type": "merger_append",
        "items": [
            {"subfeed_id": "sf_a_append", "type": "subfeed", "method_name": "a"},
            {"subfeed_id": "sf_b_append", "type": "subfeed", "method_name": "b"},
        ],
    }

    config = {
        "merger_id": "dedup_with_append_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "cursor_compress": True,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": append_config},
            {"priority": 5, "data": {"subfeed_id": "sf_c", "type": "subfeed", "method_name": "c"}},
            {"priority": 0, "data": {"subfeed_id": "sf_d", "type": "subfeed", "method_name": "d"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(config=config, methods_dict=methods_dict, user_id="u", limit=15)
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_with_append_cursor", state_backend="cursor")

    # Cursor correctness: descendant subfeed cursors exist and advance.
    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_a_append", "sf_b_append", "sf_c", "sf_d"])


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_append_and_three_sources_redis_backend(redis_client) -> None:
    a_items = [{"id": i, "src": "a"} for i in range(1, 30)]
    b_items = [{"id": i, "src": "b"} for i in range(10, 40)]
    c_items = [{"id": i, "src": "c"} for i in range(20, 60)]
    d_items = [{"id": i, "src": "d"} for i in range(25, 70)]

    methods_dict = {
        "a": make_offset_paged_method(a_items, max_per_call=1),
        "b": make_offset_paged_method(b_items, max_per_call=1),
        "c": make_offset_paged_method(c_items, max_per_call=1),
        "d": make_offset_paged_method(d_items, max_per_call=1),
    }

    append_config = {
        "merger_id": "inner_append_unused_r",
        "type": "merger_append",
        "items": [
            {"subfeed_id": "sf_a_append_r", "type": "subfeed", "method_name": "a"},
            {"subfeed_id": "sf_b_append_r", "type": "subfeed", "method_name": "b"},
        ],
    }

    config = {
        "merger_id": "dedup_with_append_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": append_config},
            {"priority": 5, "data": {"subfeed_id": "sf_c_r", "type": "subfeed", "method_name": "c"}},
            {"priority": 0, "data": {"subfeed_id": "sf_d_r", "type": "subfeed", "method_name": "d"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=15,
        redis_client_instance=redis_client,
        custom_deduplication_key="append",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_with_append_redis", state_backend="redis")

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_a_append_r", "sf_b_append_r", "sf_c_r", "sf_d_r"])


@pytest.mark.asyncio
async def test_deduplication_merger_with_percentage_cursor_backend() -> None:
    a_items = [{"id": i, "src": "pa"} for i in range(1, 60)]
    b_items = [{"id": i, "src": "pb"} for i in range(30, 90)]
    c_items = [{"id": i, "src": "pc"} for i in range(40, 120)]

    methods_dict = {
        "pa": make_offset_paged_method(a_items, max_per_call=1),
        "pb": make_offset_paged_method(b_items, max_per_call=1),
        "pc": make_offset_paged_method(c_items, max_per_call=1),
    }

    percentage_config = {
        "merger_id": "inner_percentage_unused",
        "type": "merger_percentage",
        "items": [
            {"percentage": 50, "data": {"subfeed_id": "sf_pa", "type": "subfeed", "method_name": "pa"}},
            {"percentage": 50, "data": {"subfeed_id": "sf_pb", "type": "subfeed", "method_name": "pb"}},
        ],
    }

    config = {
        "merger_id": "dedup_percentage_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "overfetch_factor": 2,
        "items": [
            {"priority": 0, "data": percentage_config},
            {"priority": 10, "data": {"subfeed_id": "sf_pc", "type": "subfeed", "method_name": "pc"}},
            {"priority": 5, "data": {"subfeed_id": "sf_pd", "type": "subfeed", "method_name": "pa"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(config=config, methods_dict=methods_dict, user_id="u", limit=20)
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_percentage_cursor", state_backend="cursor")

    for key in ("sf_pa", "sf_pb", "sf_pc"):
        assert key in res_1.next_page.data
        assert isinstance(res_1.next_page.data[key].after, int)

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_pa", "sf_pb", "sf_pc", "sf_pd"])


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_percentage_redis_backend(redis_client) -> None:
    a_items = [{"id": i, "src": "pa"} for i in range(1, 60)]
    b_items = [{"id": i, "src": "pb"} for i in range(30, 90)]
    c_items = [{"id": i, "src": "pc"} for i in range(40, 120)]

    methods_dict = {
        "pa": make_offset_paged_method(a_items, max_per_call=1),
        "pb": make_offset_paged_method(b_items, max_per_call=1),
        "pc": make_offset_paged_method(c_items, max_per_call=1),
    }

    percentage_config = {
        "merger_id": "inner_percentage_unused_r",
        "type": "merger_percentage",
        "items": [
            {"percentage": 50, "data": {"subfeed_id": "sf_pa_r", "type": "subfeed", "method_name": "pa"}},
            {"percentage": 50, "data": {"subfeed_id": "sf_pb_r", "type": "subfeed", "method_name": "pb"}},
        ],
    }

    config = {
        "merger_id": "dedup_percentage_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 0, "data": percentage_config},
            {"priority": 10, "data": {"subfeed_id": "sf_pc_r", "type": "subfeed", "method_name": "pc"}},
            {"priority": 5, "data": {"subfeed_id": "sf_pd_r", "type": "subfeed", "method_name": "pa"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=20,
        redis_client_instance=redis_client,
        custom_deduplication_key="percentage",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_percentage_redis", state_backend="redis")

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_pa_r", "sf_pb_r", "sf_pc_r", "sf_pd_r"])


@pytest.mark.asyncio
async def test_deduplication_merger_with_positional_cursor_backend() -> None:
    # MergerPositional carries its own merger cursor; verify it survives nesting in dedup.
    pos_items = [{"id": i, "src": "pos"} for i in range(1, 100)]
    def_items = [{"id": i, "src": "def"} for i in range(50, 140)]
    extra_items = [{"id": i, "src": "extra"} for i in range(80, 180)]

    methods_dict = {
        "pos": make_offset_paged_method(pos_items, max_per_call=1),
        "def": make_offset_paged_method(def_items, max_per_call=1),
        "extra": make_offset_paged_method(extra_items, max_per_call=1),
    }

    positional_config = {
        "merger_id": "inner_positional",
        "type": "merger_positional",
        "positions": [0, 2, 4, 6, 8],
        "positional": {"subfeed_id": "sf_positional", "type": "subfeed", "method_name": "pos"},
        "default": {"subfeed_id": "sf_default", "type": "subfeed", "method_name": "def"},
    }

    config = {
        "merger_id": "dedup_positional_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "overfetch_factor": 2,
        "items": [
            # Positional must run; it owns its own merger cursor entry.
            {"priority": 10, "data": positional_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra2", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(config=config, methods_dict=methods_dict, user_id="u", limit=20)
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_positional_cursor", state_backend="cursor")
    assert "inner_positional" in res_1.next_page.data
    assert "inner_positional" in res_2.next_page.data

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_positional", "sf_default", "sf_extra", "sf_extra2"])


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_positional_redis_backend(redis_client) -> None:
    pos_items = [{"id": i, "src": "pos"} for i in range(1, 100)]
    def_items = [{"id": i, "src": "def"} for i in range(50, 140)]
    extra_items = [{"id": i, "src": "extra"} for i in range(80, 180)]

    methods_dict = {
        "pos": make_offset_paged_method(pos_items, max_per_call=1),
        "def": make_offset_paged_method(def_items, max_per_call=1),
        "extra": make_offset_paged_method(extra_items, max_per_call=1),
    }

    positional_config = {
        "merger_id": "inner_positional_r",
        "type": "merger_positional",
        "positions": [0, 2, 4, 6, 8],
        "positional": {"subfeed_id": "sf_positional_r", "type": "subfeed", "method_name": "pos"},
        "default": {"subfeed_id": "sf_default_r", "type": "subfeed", "method_name": "def"},
    }

    config = {
        "merger_id": "dedup_positional_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": positional_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_r", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra2_r", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=20,
        redis_client_instance=redis_client,
        custom_deduplication_key="positional",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_positional_redis", state_backend="redis")
    assert "inner_positional_r" in res_1.next_page.data

    _assert_cursor_monotonic_if_present(
        res_1,
        res_2,
        ["sf_positional_r", "sf_default_r", "sf_extra_r", "sf_extra2_r"],
    )


@pytest.mark.asyncio
async def test_deduplication_merger_with_percentage_gradient_cursor_backend() -> None:
    from_items = [{"id": i, "src": "from"} for i in range(1, 140)]
    to_items = [{"id": i, "src": "to"} for i in range(60, 200)]
    extra_items = [{"id": i, "src": "extra"} for i in range(120, 300)]

    methods_dict = {
        "from": make_offset_paged_method(from_items, max_per_call=1),
        "to": make_offset_paged_method(to_items, max_per_call=1),
        "extra": make_offset_paged_method(extra_items, max_per_call=1),
    }

    gradient_config = {
        "merger_id": "inner_gradient",
        "type": "merger_percentage_gradient",
        "item_from": {"percentage": 80, "data": {"subfeed_id": "sf_from", "type": "subfeed", "method_name": "from"}},
        "item_to": {"percentage": 20, "data": {"subfeed_id": "sf_to", "type": "subfeed", "method_name": "to"}},
        "step": 10,
        "size_to_step": 10,
    }

    config = {
        "merger_id": "dedup_gradient_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": gradient_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_g", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra_g2", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(config=config, methods_dict=methods_dict, user_id="u", limit=25)
    _assert_two_pages_no_overlap(res_1, res_2)
    assert "inner_gradient" in res_1.next_page.data
    assert "inner_gradient" in res_2.next_page.data
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_gradient_cursor", state_backend="cursor")

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_from", "sf_to", "sf_extra_g", "sf_extra_g2"])


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_percentage_gradient_redis_backend(redis_client) -> None:
    from_items = [{"id": i, "src": "from"} for i in range(1, 140)]
    to_items = [{"id": i, "src": "to"} for i in range(60, 200)]
    extra_items = [{"id": i, "src": "extra"} for i in range(120, 300)]

    methods_dict = {
        "from": make_offset_paged_method(from_items, max_per_call=1),
        "to": make_offset_paged_method(to_items, max_per_call=1),
        "extra": make_offset_paged_method(extra_items, max_per_call=1),
    }

    gradient_config = {
        "merger_id": "inner_gradient_r",
        "type": "merger_percentage_gradient",
        "item_from": {"percentage": 80, "data": {"subfeed_id": "sf_from_r", "type": "subfeed", "method_name": "from"}},
        "item_to": {"percentage": 20, "data": {"subfeed_id": "sf_to_r", "type": "subfeed", "method_name": "to"}},
        "step": 10,
        "size_to_step": 10,
    }

    config = {
        "merger_id": "dedup_gradient_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": gradient_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_gr", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra_gr2", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=25,
        redis_client_instance=redis_client,
        custom_deduplication_key="gradient",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    assert "inner_gradient_r" in res_1.next_page.data
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_gradient_redis", state_backend="redis")

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_from_r", "sf_to_r", "sf_extra_gr", "sf_extra_gr2"])


@pytest.mark.parametrize("state_backend", ["cursor", "redis"])
@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_view_session_child(state_backend, redis_client) -> None:
    # MergerViewSession always requires Redis, so this test always uses redis_client.
    # We still validate both dedup state backends.
    base_items = [{"id": i, "src": "vs"} for i in range(1, 200)]
    extra_items = [{"id": i, "src": "extra"} for i in range(50, 260)]

    methods_dict = {
        "vs": make_offset_paged_method(base_items),
        "extra": make_offset_paged_method(extra_items),
    }

    view_session_config = {
        "merger_id": "inner_view_session",
        "type": "merger_view_session",
        "session_size": 60,
        "session_live_time": 60,
        "data": {"subfeed_id": "sf_vs", "type": "subfeed", "method_name": "vs"},
        "deduplicate": True,
        "dedup_key": "id",
    }

    config = {
        "merger_id": f"dedup_vs_{state_backend}",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": state_backend,
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": view_session_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_vs", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra_vs2", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=20,
        redis_client_instance=redis_client,
        custom_deduplication_key=f"vs_{state_backend}",
        custom_view_session_key=f"vs_{state_backend}",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id=f"dedup_vs_{state_backend}", state_backend=state_backend)
    assert "inner_view_session" in res_1.next_page.data

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_vs", "sf_extra_vs", "sf_extra_vs2"])


@pytest.mark.asyncio
async def test_deduplication_merger_with_append_distribute_cursor_backend() -> None:
    # MergerAppendDistribute (type merger_distribute) + two extra subfeeds.
    s1 = [{"id": i, "src": "s1", "group": "g1" if i % 2 == 0 else "g2"} for i in range(1, 120)]
    s2 = [{"id": i, "src": "s2", "group": "g2" if i % 3 == 0 else "g3"} for i in range(60, 200)]
    extra = [{"id": i, "src": "extra", "group": "g9"} for i in range(100, 240)]

    methods_dict = {
        "s1": make_offset_paged_method(s1, max_per_call=1),
        "s2": make_offset_paged_method(s2, max_per_call=1),
        "extra": make_offset_paged_method(extra, max_per_call=1),
    }

    distribute_config = {
        "merger_id": "inner_distribute_unused",
        "type": "merger_distribute",
        "distribution_key": "group",
        "items": [
            {"subfeed_id": "sf_s1", "type": "subfeed", "method_name": "s1"},
            {"subfeed_id": "sf_s2", "type": "subfeed", "method_name": "s2"},
        ],
    }

    config = {
        "merger_id": "dedup_distribute_cursor",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "cursor",
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": distribute_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_dist", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra_dist2", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(config=config, methods_dict=methods_dict, user_id="u", limit=25)
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_distribute_cursor", state_backend="cursor")
    for key in ("sf_s1", "sf_s2"):
        assert key in res_1.next_page.data

    _assert_cursor_monotonic_if_present(res_1, res_2, ["sf_s1", "sf_s2", "sf_extra_dist", "sf_extra_dist2"])


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_deduplication_merger_with_append_distribute_redis_backend(redis_client) -> None:
    s1 = [{"id": i, "src": "s1", "group": "g1" if i % 2 == 0 else "g2"} for i in range(1, 120)]
    s2 = [{"id": i, "src": "s2", "group": "g2" if i % 3 == 0 else "g3"} for i in range(60, 200)]
    extra = [{"id": i, "src": "extra", "group": "g9"} for i in range(100, 240)]

    methods_dict = {
        "s1": make_offset_paged_method(s1, max_per_call=1),
        "s2": make_offset_paged_method(s2, max_per_call=1),
        "extra": make_offset_paged_method(extra, max_per_call=1),
    }

    distribute_config = {
        "merger_id": "inner_distribute_unused_r",
        "type": "merger_distribute",
        "distribution_key": "group",
        "items": [
            {"subfeed_id": "sf_s1_r", "type": "subfeed", "method_name": "s1"},
            {"subfeed_id": "sf_s2_r", "type": "subfeed", "method_name": "s2"},
        ],
    }

    config = {
        "merger_id": "dedup_distribute_redis",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "state_backend": "redis",
        "state_ttl_seconds": 60,
        "overfetch_factor": 2,
        "items": [
            {"priority": 10, "data": distribute_config},
            {"priority": 5, "data": {"subfeed_id": "sf_extra_dist_r", "type": "subfeed", "method_name": "extra"}},
            {"priority": 0, "data": {"subfeed_id": "sf_extra_dist2_r", "type": "subfeed", "method_name": "extra"}},
        ],
    }

    res_1, res_2 = await _run_two_pages(
        config=config,
        methods_dict=methods_dict,
        user_id="u",
        limit=25,
        redis_client_instance=redis_client,
        custom_deduplication_key="distribute",
    )
    _assert_two_pages_no_overlap(res_1, res_2)
    _assert_dedup_backend_state(res=res_2, merger_id="dedup_distribute_redis", state_backend="redis")

    _assert_cursor_monotonic_if_present(
        res_1,
        res_2,
        ["sf_s1_r", "sf_s2_r", "sf_extra_dist_r", "sf_extra_dist2_r"],
    )
