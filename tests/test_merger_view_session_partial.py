import inspect
import json

import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerViewSessionPartial
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_VIEW_SESSION_PARTIAL_CONFIG, MERGER_VIEW_SESSION_PARTIAL_DUPS_CONFIG
from tests.fixtures.redis import redis_client


@pytest.mark.asyncio
async def test_merger_view_session_partial_no_redis() -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием без клиента Redis.
    """

    merger_vsp = MergerViewSessionPartial.parse_obj(MERGER_VIEW_SESSION_PARTIAL_CONFIG)
    with pytest.raises(ValueError):
        await merger_vsp.get_data(
            methods_dict=METHODS_DICT,
            limit=10,
            next_page=FeedResultNextPage(data={}),
            user_id="x",
        )


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
# @pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_partial(redis_client) -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием.
    """

    merger_vsp = MergerViewSessionPartial.parse_obj(MERGER_VIEW_SESSION_PARTIAL_CONFIG)
    try:
        await redis_client.delete("merger_view_session_partial_example_x")
        client_is_async = True
    except:
        client_is_async = False
        redis_client.delete("merger_view_session_partial_example_x")

    merger_vsp_res = await merger_vsp.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vsp_cache = redis_client.get(name="merger_view_session_partial_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if client_is_async:
        merger_vsp_cache = json.loads(await merger_vsp_cache)
    else:
        merger_vsp_cache = json.loads(merger_vsp_cache)

    assert merger_vsp_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10"]
    assert len(merger_vsp_cache) == len(merger_vsp_res.data)
    assert set(merger_vsp_cache[:10]) == set(merger_vsp_res.data)


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_partial_next_page(redis_client) -> None:
    """
    Тест для проверки получения данных следующей страницы из мерджера с кэшированием.
    """

    merger_vsp = MergerViewSessionPartial.parse_obj(MERGER_VIEW_SESSION_PARTIAL_CONFIG)
    try:
        await redis_client.delete("merger_view_session_partial_example_x")
        client_is_async = True
    except:
        client_is_async = False
        redis_client.delete("merger_view_session_partial_example_x")

    merger_vsp_res = await merger_vsp.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={"subfeed_merger_view_session_partial_example": FeedResultNextPageInside(page=2, after="x_10")}
        ),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vsp_cache = redis_client.get(name="merger_view_session_partial_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if client_is_async:
        merger_vsp_cache = json.loads(await merger_vsp_cache)
    else:
        merger_vsp_cache = json.loads(merger_vsp_cache)

    assert merger_vsp_res.data == ["x_11", "x_12", "x_13", "x_14", "x_15", "x_16", "x_17", "x_18", "x_19", "x_20"]
    assert len(merger_vsp_cache) == len(merger_vsp_res.data)
    assert set(merger_vsp_cache) == set(merger_vsp_res.data)


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_partial_remove_seen_ids(redis_client) -> None:
    """
    Тест для проверки удаления просмотренных идентификаторов из кэша мерджера с кэшированием.
    """

    merger_vsp = MergerViewSessionPartial.parse_obj(MERGER_VIEW_SESSION_PARTIAL_CONFIG)
    try:
        client_is_async = True
        await redis_client.delete("merger_view_session_partial_example_x")
        await redis_client.set("merger_view_session_partial_example_x", json.dumps(["x_1"]))
    except:
        client_is_async = False
        redis_client.delete("merger_view_session_partial_example_x")
        redis_client.set("merger_view_session_partial_example_x", json.dumps(["x_1"]))

    merger_vsp_res = await merger_vsp.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={"merger_view_session_partial_example": FeedResultNextPageInside(page=1)}),
        user_id="x",
        redis_client=redis_client,
    )
    merger_vsp_cache = redis_client.get(name="merger_view_session_partial_example_x")
    # Для использования синхронной и асинхронной фикстуры в одном тесте проверяем метод get
    if client_is_async:
        merger_vsp_cache = json.loads(await merger_vsp_cache)
    else:
        merger_vsp_cache = json.loads(merger_vsp_cache)

    assert merger_vsp_res.data == ["x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10", "x_11"]
    assert len(merger_vsp_cache) == len(merger_vsp_res.data) + 1
    assert set(merger_vsp_cache) == set(["x_1"] + merger_vsp_res.data)


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_merger_view_session_deduplication(redis_client) -> None:
    merger_vs = MergerViewSessionPartial.parse_obj(MERGER_VIEW_SESSION_PARTIAL_DUPS_CONFIG)
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
