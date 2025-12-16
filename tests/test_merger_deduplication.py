import inspect

import pytest

from smartfeed.schemas import (
    DeduplicationMerger,
    FeedResultClient,
    FeedResultNextPage,
    FeedResultNextPageInside,
)

from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


def make_offset_paged_method(items):
    async def _method(user_id, limit, next_page):  # pylint: disable=unused-argument
        offset = int(next_page.after or 0)
        result_data = items[offset : offset + limit]
        next_page.after = offset + len(result_data)
        next_page.page += 1
        has_next_page = (offset + len(result_data)) < len(items)
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=has_next_page)

    return _method


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

    merger = parse_model(DeduplicationMerger, config)

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

    merger = parse_model(DeduplicationMerger, config)

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

    merger = parse_model(DeduplicationMerger, config)

    res_1 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(data={}),
    )
    assert [x["id"] for x in res_1.data] == [1, 2, 3, 4, 5]

    # Simulate a full reload: page 0 requested again. Even if the client mistakenly
    # keeps the previous "after" payload, we start a new session.
    res_2 = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=5,
        next_page=FeedResultNextPage(
            data={
                "dedup_reset": FeedResultNextPageInside(page=0, after=res_1.next_page.data["dedup_reset"].after)
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

    merger = parse_model(DeduplicationMerger, config)

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
