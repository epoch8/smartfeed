import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerPositional
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_POSITIONAL_CONFIG


@pytest.mark.asyncio
async def test_merger_positional_with_positions() -> None:
    """
    Тест для проверки получения данных из позиционного мерджера на основе позиций в конфигурации.
    """

    merger_positional = MergerPositional.parse_obj(MERGER_POSITIONAL_CONFIG)
    merger_positional_res = await merger_positional.get_data(
        methods_dict=METHODS_DICT,
        limit=9,
        next_page=FeedResultNextPage(
            data={
                "subfeed_positional_merger_positional_example": FeedResultNextPageInside(page=2, after="x_10"),
                "subfeed_default_merger_positional_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_positional_res.data == ["x_11", "x_21", "x_12", "x_22", "x_23", "x_24", "x_25", "x_26", "x_27"]


@pytest.mark.asyncio
async def test_merger_positional_with_step() -> None:
    """
    Тест для проверки получения данных из позиционного мерджера на основе шагов в конфигурации.
    """

    merger_positional = MergerPositional.parse_obj(MERGER_POSITIONAL_CONFIG)
    merger_positional_res = await merger_positional.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={
                "merger_positional_example": FeedResultNextPageInside(page=3, after=None),
                "subfeed_positional_merger_positional_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_default_merger_positional_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_positional_res.data == ["x_4", "x_21", "x_5", "x_22", "x_6", "x_23", "x_7", "x_24", "x_8", "x_25"]


@pytest.mark.asyncio
async def test_merger_positional_with_empty_default() -> None:
    """
    Тест для проверки получения данных из позиционного мерджера на основе шагов в конфигурации.
    """

    merger_positional = MergerPositional.parse_obj(MERGER_POSITIONAL_CONFIG)
    merger_positional.default.method_name = "empty"
    merger_positional_res = await merger_positional.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={
                "merger_positional_example": FeedResultNextPageInside(page=3, after=None),
                "subfeed_positional_merger_positional_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_default_merger_positional_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_positional_res.data == ["x_4", "x_5", "x_6", "x_7", "x_8"]
