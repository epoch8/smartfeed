import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerPercentage
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_PERCENTAGE_CONFIG


@pytest.mark.asyncio
async def test_merger_percentage() -> None:
    """
    Тест для проверки получения данных из процентного мерджера.
    """

    merger_percentage = MergerPercentage.parse_obj(MERGER_PERCENTAGE_CONFIG)
    merger_percentage_res = await merger_percentage.get_data(
        methods_dict=METHODS_DICT,
        limit=10,
        next_page=FeedResultNextPage(
            data={
                "subfeed_merger_percentage_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_2_merger_percentage_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert merger_percentage_res.data == ["x_4", "x_21", "x_22", "x_5", "x_23", "x_24", "x_6", "x_25", "x_26", "x_7"]
