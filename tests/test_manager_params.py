import pytest

from smartfeed.manager import FeedManager
from smartfeed.schemas import FeedResultClient, FeedResultNextPage, FeedResultNextPageInside


async def meta_method(
    user_id: str,
    limit: int,
    next_page: FeedResultNextPageInside,
    meta: dict,
) -> FeedResultClient:
    assert meta["tag"] == "alpha"
    take = int(meta.get("take", limit))
    data = [f"{user_id}:{meta['tag']}"] * min(limit, take)
    next_page.after = None
    next_page.page += 1
    return FeedResultClient(data=data, next_page=next_page, has_next_page=False)


@pytest.mark.asyncio
async def test_manager_passes_params_to_subfeed() -> None:
    config = {
        "version": "1",
        "feed": {
            "subfeed_id": "sf_meta",
            "type": "subfeed",
            "method_name": "meta_method",
        },
    }

    manager = FeedManager(config=config, methods_dict={"meta_method": meta_method})
    result = await manager.get_data(
        user_id="u1",
        limit=5,
        next_page=FeedResultNextPage(data={}),
        meta={"tag": "alpha", "take": 2},
    )

    assert result.data == ["u1:alpha", "u1:alpha"]
