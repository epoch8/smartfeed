"""Regression test: view-session dedup must keep the higher-priority copy.

Reproduces the production feed structure (dedup -> positional -> view_session ->
percentage). A tour present in both the recommended (dedup_priority=2) and the
regular (dedup_priority=1) streams shares the same id. The session-level dedup
used to keep whichever copy came first by POSITION; the 35/65 percentage
interleave makes the lower-priority regular stream reach shared ids first, so the
recommended copy was dropped and recommended collapsed far below its 35% quota.
Dedup must honour dedup_priority instead.
"""

import inspect

import pytest

from smartfeed.manager import FeedManager
from smartfeed.schemas import FeedResultClient, FeedResultNextPage
from tests.fixtures.redis import redis_client  # noqa: F401


def _stream(src, ids):
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


async def _promo(user_id, limit, next_page, **kwargs):
    start = int(next_page.after) + 1 if next_page.after else 9000
    ids = list(range(start, start + limit))
    next_page.after = str(ids[-1])
    next_page.page += 1
    return FeedResultClient(
        data=[{"id": i, "src": "promo"} for i in ids],
        next_page=next_page,
        has_next_page=True,
    )


# recommended pool 1000..1399; regular pool 1000..1099 (overlap) + 3000..4999 (unique)
RECOMMENDED_IDS = list(range(1000, 1400))
REGULAR_IDS = list(range(1000, 1100)) + list(range(3000, 5000))

METHODS = {
    "recommended_tours": _stream("rec", RECOMMENDED_IDS),
    "regular_tours": _stream("reg", REGULAR_IDS),
    "promo_tours": _promo,
}

# Production feed shape: dedup -> positional -> view_session(deduplicate) -> percentage(35/65).
CONFIG = {
    "version": "1",
    "feed": {
        "merger_id": "pdt_dedup",
        "type": "merger_deduplication",
        "dedup_key": "id",
        "missing_key_policy": "error",
        "state_backend": "redis",
        "state_ttl_seconds": 300,
        "cursor_compress": True,
        "overfetch_factor": 5,
        "max_refill_loops": 2,
        "data": {
            "merger_id": "pdt_main",
            "type": "merger_positional",
            "positions": [1, 3, 7, 9],
            "positional": {
                "subfeed_id": "promo_tours",
                "type": "subfeed",
                "method_name": "promo_tours",
                "dedup_priority": 3,
                "subfeed_params": {},
            },
            "default": {
                "merger_id": "pdt_session",
                "type": "merger_view_session",
                "session_size": 300,
                "session_live_time": 300,
                "deduplicate": True,
                "dedup_key": "id",
                "data": {
                    "merger_id": "pdt_pct",
                    "type": "merger_percentage",
                    "shuffle": False,
                    "items": [
                        {
                            "percentage": 35,
                            "data": {
                                "merger_id": "pdt_smsession",
                                "type": "merger_view_session",
                                "session_size": 500,
                                "session_live_time": 300,
                                "deduplicate": True,
                                "dedup_key": "id",
                                "data": {
                                    "subfeed_id": "recommended_tours",
                                    "type": "subfeed",
                                    "method_name": "recommended_tours",
                                    "dedup_priority": 2,
                                    "subfeed_params": {},
                                },
                            },
                        },
                        {
                            "percentage": 65,
                            "data": {
                                "subfeed_id": "regular_tours",
                                "type": "subfeed",
                                "method_name": "regular_tours",
                                "dedup_priority": 1,
                                "subfeed_params": {},
                            },
                        },
                    ],
                },
            },
        },
    },
}


async def _del(redis_client, *keys):  # noqa: F811
    for key in keys:
        res = redis_client.delete(key)
        if inspect.iscoroutine(res):
            await res


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_view_session_dedup_keeps_higher_priority_copy(redis_client) -> None:  # noqa: F811
    user_id = "pdt_user"

    # Clean every cache/state key this feed uses so we exercise a fresh build.
    await _del(
        redis_client,
        f"pdt_session_{user_id}",
        f"pdt_session_{user_id}:meta",
        f"pdt_smsession_{user_id}",
        f"pdt_smsession_{user_id}:meta",
        f"dedup:pdt_dedup:{user_id}",
    )

    fm = FeedManager(config=CONFIG, methods_dict=METHODS, redis_client=redis_client)

    seen: set = set()
    rec_count = 0
    non_promo = 0
    cursor = FeedResultNextPage(data={})
    for _ in range(15):
        res = await fm.get_data(user_id=user_id, limit=20, next_page=cursor)
        for it in res.data:
            assert it["id"] not in seen, f"duplicate id {it['id']} across pages"
            seen.add(it["id"])
            if it["src"] == "rec":
                rec_count += 1
            if it["src"] in ("rec", "reg"):
                non_promo += 1
        if not res.has_next_page:
            break
        cursor = res.next_page

    share = rec_count / non_promo if non_promo else 0
    # Position-blind dedup collapsed recommended to ~13%; priority-aware dedup keeps it near 35%.
    assert share >= 0.30, f"recommended starved by overlap: {share:.0%} of non-promo (expected ~35%)"
