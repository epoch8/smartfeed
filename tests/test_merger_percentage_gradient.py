import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerPercentageGradient
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_PERCENTAGE_GRADIENT_CONFIG


@pytest.mark.asyncio
async def test_merger_percentage_gradient() -> None:
    """
    Тест для проверки получения данных из процентного мерджера с градиентом.
    """

    merger_percentage_gradient = MergerPercentageGradient.parse_obj(MERGER_PERCENTAGE_GRADIENT_CONFIG)
    merger_percentage_gradient_res = await merger_percentage_gradient.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={
                "subfeed_from_merger_percentage_gradient_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_to_merger_percentage_gradient_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_percentage_gradient_res.data == [
        "x_4",
        "x_5",
        "x_6",
        "x_7",
        "x_8",
        "x_9",
        "x_10",
        "x_11",
        "x_21",
        "x_22",
    ]


@pytest.mark.asyncio
async def test_merger_percentage_gradient_next_page() -> None:
    """
    Тест для проверки получения данных из процентного мерджера с градиентом после изменения процента на другой странице.
    """

    merger_percentage_gradient = MergerPercentageGradient.parse_obj(MERGER_PERCENTAGE_GRADIENT_CONFIG)
    merger_percentage_gradient_res = await merger_percentage_gradient.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={
                "merger_percentage_gradient_example": FeedResultNextPageInside(page=2, after=None),
                "subfeed_from_merger_percentage_gradient_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_to_merger_percentage_gradient_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_percentage_gradient_res.data == [
        "x_4",
        "x_5",
        "x_6",
        "x_7",
        "x_8",
        "x_9",
        "x_10",
        "x_21",
        "x_22",
        "x_23",
    ]
