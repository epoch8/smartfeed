import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerAppend
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_APPEND_CONFIG


@pytest.mark.asyncio
async def test_merger_append() -> None:
    """
    Тест для проверки получения данных из append мерджера.
    """

    merger_append = MergerAppend.parse_obj(MERGER_APPEND_CONFIG)
    merger_append_res = await merger_append.get_data(
        methods_dict=METHODS_DICT,
        limit=11,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
    )

    assert merger_append_res.data == ["x_1", "x_2", "x_3", "x_4", "x_5", "x_1", "x_2", "x_3", "x_4", "x_5", "x_6"]


@pytest.mark.asyncio
async def test_merger_append_with_item_1_page_2() -> None:
    """
    Тест для проверки получения данных из append мерджера с курсором пагинации первого субфида.
    """

    merger_append = MergerAppend.parse_obj(MERGER_APPEND_CONFIG)
    merger_append_res = await merger_append.get_data(
        methods_dict=METHODS_DICT,
        limit=11,
        next_page=FeedResultNextPage(
            data={"subfeed_merger_append_example": FeedResultNextPageInside(page=2, after="x_5")}
        ),
        user_id="x",
    )

    assert merger_append_res.data == ["x_6", "x_7", "x_8", "x_9", "x_10", "x_1", "x_2", "x_3", "x_4", "x_5", "x_6"]
    assert merger_append_res.next_page.data["subfeed_merger_append_example"].page == 3
    assert merger_append_res.next_page.data["subfeed_merger_append_example"].after == "x_10"
