import pytest

from smartfeed.schemas import FeedResultNextPage, SubFeed
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.subfeeds import (
    SUBFEED_CONFIG,
    SUBFEED_CONFIG_NO_RAISE_ERROR,
    SUBFEED_CONFIG_RAISE_ERROR,
    SUBFEED_WITH_PARAMS_CONFIG,
)


@pytest.mark.asyncio
async def test_sub_feed() -> None:
    """
    Тест для проверки получения данных из субфида (без параметров).
    """

    sub_feed = SubFeed.parse_obj(SUBFEED_CONFIG)
    sub_feed_data = await sub_feed.get_data(
        methods_dict=METHODS_DICT,
        limit=15,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
    )

    assert sub_feed_data.data == [f"x_{i}" for i in range(1, 16)]


@pytest.mark.asyncio
async def test_sub_feed_with_params() -> None:
    """
    Тест для проверки получения данных из субфида (с параметрами).
    """

    sub_feed = SubFeed.parse_obj(SUBFEED_WITH_PARAMS_CONFIG)
    sub_feed_data = await sub_feed.get_data(
        methods_dict=METHODS_DICT,
        limit=15,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
    )

    assert sub_feed_data.data == [f"x_{i}" for i in range(1, 11)]


@pytest.mark.asyncio
async def test_sub_feed_raise_error() -> None:
    """
    Тест для проверки получения данных из субфида (без параметров).
    """

    sub_feed = SubFeed.parse_obj(SUBFEED_CONFIG_RAISE_ERROR)

    with pytest.raises(Exception):
        await sub_feed.get_data(
            methods_dict=METHODS_DICT,
            limit=15,
            next_page=FeedResultNextPage(data={}),
            user_id="x",
        )


@pytest.mark.asyncio
async def test_sub_feed_no_raise_error() -> None:
    """
    Тест для проверки получения данных из субфида (без параметров).
    """

    sub_feed = SubFeed.parse_obj(SUBFEED_CONFIG_NO_RAISE_ERROR)
    sub_feed_data = await sub_feed.get_data(
        methods_dict=METHODS_DICT,
        limit=15,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
    )

    assert sub_feed_data.data == []
