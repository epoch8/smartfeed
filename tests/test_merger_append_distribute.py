import pytest

from smartfeed.schemas import FeedResultNextPage, FeedResultNextPageInside, MergerAppendDistribute
from tests.fixtures.configs import METHODS_DICT
from tests.fixtures.mergers import MERGER_APPEND_DISTRIBUTE_CONFIG


@pytest.mark.asyncio
async def test_merger_disturbed_append() -> None:
    """
    Тест для проверки получения данных из append мерджера.
    """

    merger_distributed = MergerAppendDistribute.parse_obj(MERGER_APPEND_DISTRIBUTE_CONFIG)
    merger_distributed_res = await merger_distributed.get_data(
        methods_dict=METHODS_DICT,
        limit=20,
        next_page=FeedResultNextPage(data={}),
        user_id="x",
    )
    for i in range(len(merger_distributed_res.data) - 1):
        assert (
            merger_distributed_res.data[i][merger_distributed.distribution_key]
            != merger_distributed_res.data[i + 1][merger_distributed.distribution_key]
        )


@pytest.mark.asyncio
async def test_merger_append_with_item_1_page_2() -> None:
    """
    Тест для проверки получения данных из append мерджера с курсором пагинации первого субфида.
    """
    merger_distributed = MergerAppendDistribute.parse_obj(MERGER_APPEND_DISTRIBUTE_CONFIG)
    merger_distributed_res = await merger_distributed.get_data(
        methods_dict=METHODS_DICT,
        limit=11,
        next_page=FeedResultNextPage(
            data={
                "subfeed_merger_distribute_example": FeedResultNextPageInside(
                    page=2, after={"user_id": "x_1", "value": 11}
                )
            }
        ),
        user_id="x",
    )
    for i in range(len(merger_distributed_res.data) - 1):
        assert (
            merger_distributed_res.data[i][merger_distributed.distribution_key]
            != merger_distributed_res.data[i + 1][merger_distributed.distribution_key]
        )
    assert merger_distributed_res.next_page.data["subfeed_merger_distribute_example"].page == 3
    assert merger_distributed_res.next_page.data["subfeed_merger_distribute_example"].after == {
        "user_id": "x_2",
        "value": 22,
    }
