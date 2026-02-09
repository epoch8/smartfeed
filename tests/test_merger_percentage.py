import copy

import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerPercentage
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_PERCENTAGE_CONFIG
from tests.utils import parse_model


@pytest.mark.asyncio
async def test_merger_percentage() -> None:
    """
    Тест для проверки получения данных из процентного мерджера.
    """

    merger_percentage = parse_model(MergerPercentage, MERGER_PERCENTAGE_CONFIG)
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


@pytest.mark.asyncio
async def test_merger_percentage_when_one_leaf_is_empty() -> None:
    config = copy.deepcopy(MERGER_PERCENTAGE_CONFIG)
    # Make the second leaf return no data + has_next_page=False.
    config["items"][1]["data"]["method_name"] = "empty"

    merger_percentage = parse_model(MergerPercentage, config)
    res = await merger_percentage.get_data(
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

    assert res.data == ["x_4", "x_5", "x_6", "x_7"]
    # The non-empty leaf still reports more pages.
    assert res.has_next_page is True


@pytest.mark.asyncio
async def test_merger_percentage_odd_limit_fills_page_when_sources_have_data() -> None:
    merger_percentage = parse_model(MergerPercentage, MERGER_PERCENTAGE_CONFIG)
    res = await merger_percentage.get_data(
        methods_dict=METHODS_DICT,
        limit=11,
        next_page=FeedResultNextPage(
            data={
                "subfeed_merger_percentage_example": FeedResultNextPageInside(page=2, after="x_3"),
                "subfeed_2_merger_percentage_example": FeedResultNextPageInside(page=3, after="x_20"),
            }
        ),
        user_id="x",
    )

    assert len(res.data) == 11
