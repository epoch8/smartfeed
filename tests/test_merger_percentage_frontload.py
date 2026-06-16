"""MergerPercentage must front-load the target ratio.

When a source has fewer items than its target share, the mix should keep the
TARGET percentage on the early pages (consuming that source first) and only then
fall back to the other source -- rather than smearing the scarce source thinly
across the whole feed. With 35/65 and a scarce "rec" source, the first pages
must hold ~35% rec, then drop to 0 once rec is exhausted.
"""

import pytest

from smartfeed.schemas import FeedResultNextPage, MergerPercentage
from tests.utils import parse_model


CONFIG = {
    "merger_id": "fl",
    "type": "merger_percentage",
    "shuffle": False,
    "items": [
        {
            "percentage": 35,
            "data": {"subfeed_id": "rec", "type": "subfeed", "method_name": "rec"},
        },
        {
            "percentage": 65,
            "data": {"subfeed_id": "reg", "type": "subfeed", "method_name": "reg"},
        },
    ],
}


def _make_methods(rec_count, reg_count):
    from smartfeed.schemas import FeedResultClient

    def stream(prefix, count):
        async def method(user_id, limit, next_page, **kwargs):
            start = int(next_page.after.split("_")[1]) + 1 if next_page.after else 0
            ids = list(range(start, min(start + limit, count)))
            data = [f"{prefix}_{i}" for i in ids]
            next_page.after = data[-1] if data else None
            next_page.page += 1
            return FeedResultClient(
                data=data, next_page=next_page, has_next_page=(ids[-1] + 1 < count) if ids else False
            )

        return method

    return {"rec": stream("rec", rec_count), "reg": stream("reg", reg_count)}


@pytest.mark.asyncio
async def test_percentage_frontloads_scarce_source() -> None:
    # 30 rec available, plenty of reg; assemble 200 items in 20-item pages.
    merger = parse_model(MergerPercentage, CONFIG)
    methods = _make_methods(rec_count=30, reg_count=1000)

    res = await merger.get_data(
        methods_dict=methods,
        limit=200,
        next_page=FeedResultNextPage(data={}),
        user_id="u",
    )
    data = res.data

    pages = [data[i : i + 20] for i in range(0, len(data), 20)]
    rec_per_page = [sum(1 for x in pg if x.startswith("rec")) for pg in pages]

    # Early pages hold the target ~35% (7 of 20); once rec is exhausted, they drop to 0.
    assert rec_per_page[0] >= 6, f"first page should hold ~35% rec, got {rec_per_page[0]}"
    assert rec_per_page[1] >= 6, f"second page should hold ~35% rec, got {rec_per_page[1]}"
    # All 30 rec consumed within the first ~5 pages, tail is pure reg.
    assert sum(rec_per_page[:5]) == 30, f"rec should be front-loaded into early pages, got {rec_per_page}"
    assert rec_per_page[-1] == 0, f"tail pages should be pure reg, got {rec_per_page}"
