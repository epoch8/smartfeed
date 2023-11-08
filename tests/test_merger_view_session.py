import json

import pytest
import redis

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerViewSession
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_VIEW_SESSION_CONFIG

REDIS_CLIENT = redis.Redis(host="localhost", port=6379, db=0)


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


@pytest.mark.asyncio
async def test_merger_view_session() -> None:
    """
    Тест для проверки получения данных из мерджера с кэшированием.
    """

    merger_vs = MergerViewSession.parse_obj(MERGER_VIEW_SESSION_CONFIG)
    merger_vs_res = await merger_vs.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
        redis_client=REDIS_CLIENT,
    )
    merger_vs_cache = json.loads(REDIS_CLIENT.get(name="merger_view_session_example_x"))  # type: ignore

    assert merger_vs_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_6", "x_7", "x_8", "x_9", "x_10"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[:10] == merger_vs_res.data


@pytest.mark.asyncio
async def test_merger_view_session_next_page() -> None:
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
        redis_client=REDIS_CLIENT,
    )
    merger_vs_cache = json.loads(REDIS_CLIENT.get(name="merger_view_session_example_x"))  # type: ignore

    assert merger_vs_res.data == ["x_11", "x_12", "x_13", "x_14", "x_15", "x_16", "x_17", "x_18", "x_19", "x_20"]
    assert len(merger_vs_cache) == merger_vs.session_size
    assert merger_vs_cache[10:20] == merger_vs_res.data