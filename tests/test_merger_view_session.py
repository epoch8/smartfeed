import inspect
import json

import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerViewSession
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_VIEW_SESSION_CONFIG, MERGER_VIEW_SESSION_DUPS_CONFIG
from tests.fixtures.redis import async_redis_client, redis_client


@pytest.mark.asyncio
async def test_merger_view_session_no_redis() -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием без клиента Redis.
    """

    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
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

    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = redis_client.get(name="merger_view_session_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if inspect.iscoroutine(merger_vs_cache):
        merger_vs_cache = json.loads(await merger_vs_cache)
    else:
        merger_vs_cache = json.loads(merger_vs_cache)

    assert merger_vs_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_next_page(redis_client) -> None:
    """
    Тест для проверки получения данных следующей страницы из мерджера с кэшированием.
    """

    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={"merger_view_session_example": FeedResultNextPageInside(page=2, after=None)}
        ),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = redis_client.get(name="merger_view_session_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if inspect.iscoroutine(merger_vs_cache):
        merger_vs_cache = json.loads(await merger_vs_cache)
    else:
        merger_vs_cache = json.loads(merger_vs_cache)

    assert merger_vs_res.data == ["x_11", "x_12", "x_13", "x_14", "x_15", "x_16", "x_17", "x_18", "x_19", "x_20"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[10:20] == merger_vs_res.data


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_deduplication(redis_client) -> None:
    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_DUPS_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vs_cache = redis_client.get(name="merger_view_session_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if inspect.iscoroutine(merger_vs_cache):
        merger_vs_cache = json.loads(await merger_vs_cache)
    else:
        merger_vs_cache = json.loads(merger_vs_cache)

    assert merger_vs_res.data == [i for i in range(1, 11)]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data
