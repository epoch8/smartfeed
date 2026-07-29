from smartfeed.models.base import FeedResult


async def make_items(user_id: str, limit: int, next_page: dict, **kwargs) -> FeedResult:
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    data = [{"id": start + i, "title": f"item_{start + i}"} for i in range(limit)]
    return FeedResult(
        data=data,
        next_page={"page": page + 1},
        has_next_page=True,
    )


async def make_empty(user_id: str, limit: int, next_page: dict, **kwargs) -> FeedResult:
    return FeedResult(data=[], next_page=next_page, has_next_page=False)


async def make_error(user_id: str, limit: int, next_page: dict, **kwargs) -> FeedResult:
    raise RuntimeError("subfeed error")


METHODS = {
    "items": make_items,
    "empty": make_empty,
    "error": make_error,
}
