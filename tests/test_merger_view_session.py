import json

import pytest

from smartfeed.feed_models import _redis_call
from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerDeduplication, MergerViewSession
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import (
    MERGER_DEDUP_VIEW_SESSION_CONFIG,
    MERGER_VIEW_SESSION_CONFIG,
    MERGER_VIEW_SESSION_DUPS_CONFIG,
)
from tests.fixtures.redis import redis_client  # noqa: F401
from tests.utils import parse_model


async def _get_cache_json(redis_client, key: str):
    return json.loads(await _redis_call(redis_client, "get", key))


@pytest.mark.asyncio
async def test_merger_view_session_no_redis() -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием без клиента Redis.
    """

    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_CONFIG)
    with pytest.raises(ValueError):
        await merger_vs.get_data(
            methods_dict=METHODS_DICT,
            limit=10,
            next_page=FeedResultNextPage(data={}),
            user_id="x",
        )


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session(redis_client) -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием.
    """

    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = await _get_cache_json(redis_client, "merger_view_session_example_x")

    assert merger_vs_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_custom_key(redis_client) -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием по ключу с кастомным постфиксом.
    """

    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_CONFIG)
    # Даем дополнительный параметр, который мерджер добавит в ключ кэша.
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
        custom_view_session_key="foo",
    )
    merger_vs_cache = await _get_cache_json(redis_client, "merger_view_session_example_x_foo")

    assert merger_vs_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_next_page(redis_client) -> None:
    """
    Тест для проверки получения данных следующей страницы из мерджера с кэшированием.
    """

    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={"merger_view_session_example": FeedResultNextPageInside(page=2, after=None)}
        ),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = await _get_cache_json(redis_client, "merger_view_session_example_x")

    assert merger_vs_res.data == ["x_11", "x_12", "x_13", "x_14", "x_15", "x_16", "x_17", "x_18", "x_19", "x_20"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[10:20] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_deduplication(redis_client) -> None:
    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_DUPS_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = await _get_cache_json(redis_client, "merger_view_session_example_x")

    assert merger_vs_res.data == [i for i in range(1, 11)]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_directly_over_view_session(redis_client) -> None:
    """Regression: dedup context must not leak into view session cache generation."""
    merger = parse_model(MergerDeduplication, MERGER_DEDUP_VIEW_SESSION_CONFIG)
    result = await merger.get_data(
        methods_dict=METHODS_DICT,
        limit=20,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    # Currently returns 0 items — all rejected as "seen" during cache build
    assert len(result.data) == 20
    assert result.data == [f"x_{i}" for i in range(1, 21)]


MERGER_VIEW_SESSION_SMALL_CONFIG = {
    "merger_id": "small_session",
    "type": "merger_view_session",
    "session_size": 20,
    "session_live_time": 300,
    "data": {
        "subfeed_id": "subfeed_small_session",
        "type": "subfeed",
        "method_name": "followings",
    },
}

MERGER_DEDUP_SMALL_SESSION_CONFIG = {
    "merger_id": "dedup_small",
    "type": "merger_deduplication",
    "dedup_key": None,
    "max_refill_loops": 3,
    "data": {
        "merger_id": "small_session_dedup",
        "type": "merger_view_session",
        "session_size": 30,
        "session_live_time": 300,
        "data": {
            "subfeed_id": "subfeed_dedup_small",
            "type": "subfeed",
            "method_name": "followings",
        },
    },
}

MERGER_DEDUP_LARGE_POOL_CONFIG = {
    "merger_id": "dedup_large",
    "type": "merger_deduplication",
    "dedup_key": None,
    "max_refill_loops": 3,
    "data": {
        "merger_id": "large_pool_session",
        "type": "merger_view_session",
        "session_size": 300,
        "session_live_time": 300,
        "data": {
            "subfeed_id": "subfeed_large_pool",
            "type": "subfeed",
            "method_name": "large",
        },
    },
}


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_session_rebuild_continues_pagination(redis_client) -> None:
    """Pagination continues beyond session_size via session rebuild.

    session_size=20, limit=10. After 2 pages (20 items), session exhausts.
    has_next_page should remain True because child has more data.
    Page 3 triggers rebuild and returns fresh items.
    """
    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_SMALL_CONFIG)
    user_id = "rebuild_test"

    all_items = []
    next_page = FeedResultNextPage(data={})
    num_pages = 5  # 50 items, crosses 2+ sessions of 20

    for page_num in range(1, num_pages + 1):
        result = await merger_vs.get_data(
            methods_dict=METHODS_DICT,
            limit=10,
            next_page=next_page,
            user_id=user_id,
            redis_client=redis_client,
        )
        assert len(result.data) == 10, f"Page {page_num}: expected 10 items, got {len(result.data)}"
        all_items.extend(result.data)
        next_page = result.next_page

        if page_num < num_pages:
            assert result.has_next_page, (
                f"Page {page_num}: has_next_page=False but expected True "
                f"(session_size=20, should rebuild)"
            )

    # 50 unique items across 2+ sessions
    assert len(all_items) == 50
    assert all_items == [f"{user_id}_{i}" for i in range(1, 51)]
    # No duplicates
    assert len(set(all_items)) == 50


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_session_has_next_page_true_at_boundary(redis_client) -> None:
    """has_next_page=True when session exactly exhausted but child has more.

    session_size=20, limit=10. Page 2 returns items 11-20 (session exhausted).
    has_next_page must be True because child can rebuild.
    """
    merger_vs = parse_model(MergerViewSession, MERGER_VIEW_SESSION_SMALL_CONFIG)
    user_id = "boundary_test"

    # Page 1
    result1 = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id=user_id,
        redis_client=redis_client,
    )
    assert result1.has_next_page is True
    assert result1.data == [f"{user_id}_{i}" for i in range(1, 11)]

    # Page 2 -- session exactly exhausted (items 11-20, session_size=20)
    result2 = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=result1.next_page,
        user_id=user_id,
        redis_client=redis_client,
    )
    assert result2.data == [f"{user_id}_{i}" for i in range(11, 21)]
    # KEY ASSERTION: has_next_page=True despite session exhausted
    assert result2.has_next_page is True, (
        "has_next_page should be True when session exhausted but child has more data"
    )

    # Page 3 -- rebuild triggers, fresh items 21-30
    result3 = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=result2.next_page,
        user_id=user_id,
        redis_client=redis_client,
    )
    assert len(result3.data) == 10
    assert result3.data == [f"{user_id}_{i}" for i in range(21, 31)]


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_over_session_no_duplicates_across_rebuilds(redis_client) -> None:
    """dedup + session_size=30: пагинация до 900 туров, 0 дупликатов.

    session_size=30 при limit=10 дает 3 страницы на сессию.
    90 страниц = 30 пересборок сессии.
    Все 900 туров должны быть уникальными (dedup трекает виденные).
    (example_method генерирует 999 элементов, берем 900 чтобы не упереться в лимит)
    """
    merger = parse_model(MergerDeduplication, MERGER_DEDUP_SMALL_SESSION_CONFIG)
    user_id = "dedup_900"
    limit = 10
    num_pages = 90

    all_items = []
    next_page = FeedResultNextPage(data={})

    for page_num in range(1, num_pages + 1):
        result = await merger.get_data(
            methods_dict=METHODS_DICT,
            limit=limit,
            next_page=next_page,
            user_id=user_id,
            redis_client=redis_client,
        )
        assert len(result.data) == limit, (
            f"Page {page_num}: expected {limit} items, got {len(result.data)}"
        )
        all_items.extend(result.data)
        next_page = result.next_page

        if page_num < num_pages:
            assert result.has_next_page, (
                f"Page {page_num}: has_next_page=False, got only {len(all_items)} items"
            )

    assert len(all_items) == 900
    unique = set(all_items)
    assert len(unique) == 900, (
        f"Found {900 - len(unique)} duplicates among 900 items"
    )


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_dedup_over_session_1500_unique_tours(redis_client) -> None:
    """dedup + session_size=300, pool=5000: 1500 unique tours, 0 duplicates.

    session_size=300 with limit=150 gives 2 pages per session.
    10 pages = 5 session rebuilds = 1500 tours.
    All must be unique (dedup tracks seen items across sessions).
    """
    merger = parse_model(MergerDeduplication, MERGER_DEDUP_LARGE_POOL_CONFIG)
    user_id = "dedup_1500"
    limit = 150
    num_pages = 10

    all_items = []
    next_page = FeedResultNextPage(data={})

    for page_num in range(1, num_pages + 1):
        result = await merger.get_data(
            methods_dict=METHODS_DICT,
            limit=limit,
            next_page=next_page,
            user_id=user_id,
            redis_client=redis_client,
        )
        assert len(result.data) == limit, (
            f"Page {page_num}: expected {limit} items, got {len(result.data)}"
        )
        all_items.extend(result.data)
        next_page = result.next_page

        if page_num < num_pages:
            assert result.has_next_page, (
                f"Page {page_num}: has_next_page=False after only {len(all_items)} items"
            )

    assert len(all_items) == 1500
    unique = set(all_items)
    assert len(unique) == 1500, (
        f"Found {1500 - len(unique)} duplicates among 1500 items"
    )
