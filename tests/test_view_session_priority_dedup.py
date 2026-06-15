"""Regression test: view-session dedup must keep the higher-priority copy.

When two sub-feeds mixed by MergerPercentage emit the SAME item id but with
different ``dedup_priority`` (e.g. recommended=2 vs regular=1), the session-level
deduplication used to keep whichever copy came first by position. The percentage
interleave makes the lower-priority stream reach shared ids first, so the
higher-priority copy was dropped -- recommended items collapsed far below the
configured percentage. The dedup must instead honour dedup_priority.
"""

import fakeredis
import pytest

from smartfeed.manager import FeedManager
from smartfeed.schemas import FeedResultClient, FeedResultNextPage


def _stream(src: str, ids):
    async def method(user_id, limit, next_page, **kwargs):
        start = int(next_page.after) + 1 if next_page.after else ids[0]
        batch = [i for i in ids if i >= start][:limit]
        next_page.after = str(batch[-1]) if batch else None
        next_page.page += 1
        has_next = bool(batch) and batch[-1] != ids[-1]
        return FeedResultClient(
            data=[{"id": i, "src": src} for i in batch],
            next_page=next_page,
            has_next_page=has_next,
        )

    return method


# recommended pool 1000..1199 (plenty); regular pool 1000..1099 (overlap) + 3000..3999 (unique)
RECOMMENDED_IDS = list(range(1000, 1200))
REGULAR_IDS = list(range(1000, 1100)) + list(range(3000, 4000))

METHODS = {
    "recommended": _stream("rec", RECOMMENDED_IDS),
    "regular": _stream("reg", REGULAR_IDS),
}

# view_session(deduplicate) over percentage(50% recommended prio=2 / 50% regular prio=1)
CONFIG = {
    "version": "1",
    "feed": {
        "merger_id": "session",
        "type": "merger_view_session",
        "session_size": 100,
        "session_live_time": 300,
        "deduplicate": True,
        "dedup_key": "id",
        "data": {
            "merger_id": "mix",
            "type": "merger_percentage",
            "shuffle": False,
            "items": [
                {
                    "percentage": 35,
                    "data": {
                        "subfeed_id": "recommended",
                        "type": "subfeed",
                        "method_name": "recommended",
                        "dedup_priority": 2,
                        "subfeed_params": {},
                    },
                },
                {
                    "percentage": 65,
                    "data": {
                        "subfeed_id": "regular",
                        "type": "subfeed",
                        "method_name": "regular",
                        "dedup_priority": 1,
                        "subfeed_params": {},
                    },
                },
            ],
        },
    },
}


@pytest.mark.asyncio
async def test_view_session_dedup_keeps_higher_priority_copy() -> None:
    fm = FeedManager(config=CONFIG, methods_dict=METHODS, redis_client=fakeredis.FakeStrictRedis())

    res = await fm.get_data(user_id="u1", limit=100, next_page=FeedResultNextPage(data={}))
    items = res.data

    ids = [it["id"] for it in items]
    rec_count = sum(1 for it in items if it["src"] == "rec")

    # No duplicate ids survive the session dedup.
    assert len(ids) == len(set(ids)), "duplicate ids in deduped session"

    # Recommended (35% quota) must not be starved by the id overlap with regular.
    # The position-blind dedup collapsed it to a handful; priority-aware dedup keeps it near 35%.
    assert rec_count >= 30, f"recommended starved by overlap: only {rec_count}/100 (expected ~35)"
