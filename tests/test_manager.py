"""FeedManager-level tests: the sole public entrypoint was
previously exercised only indirectly through executor.run().
"""

import pytest
from pydantic import ValidationError

from smartfeed.manager import FeedManager
from smartfeed.models.base import FeedResult


async def make_items(user_id, limit, next_page, **kw):
    page = next_page.get("page", 1)
    start = (page - 1) * limit
    return FeedResult(
        data=[{"id": start + i} for i in range(limit)],
        next_page={"page": page + 1},
        has_next_page=True,
    )


CONFIG = {
    "feed": {
        "type": "merger_percentage",
        "node_id": "mix",
        "items": [
            {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "a", "method_name": "items"}},
            {"percentage": 50, "data": {"type": "subfeed", "subfeed_id": "b", "method_name": "items"}},
        ],
    }
}


def test_malformed_config_raises_at_construction():
    with pytest.raises(ValidationError):
        FeedManager(config={"feed": {"type": "wrapper"}}, methods_dict={})  # node_id/data missing


def test_unknown_node_type_raises_at_construction():
    with pytest.raises(ValidationError):
        FeedManager(config={"feed": {"type": "no_such_node"}}, methods_dict={})


@pytest.mark.asyncio
async def test_positions_stamped_0_based_contiguous():
    mgr = FeedManager(config=CONFIG, methods_dict={"items": make_items})
    r = await mgr.get_feed(session_id="u", limit=10)
    positions = [it["_smartfeed_debug_info"]["smartfeed_position"] for it in r.data]
    assert positions == list(range(len(r.data)))
    assert len(r.data) == 10


@pytest.mark.asyncio
async def test_cursor_round_trip_through_manager():
    mgr = FeedManager(config=CONFIG, methods_dict={"items": make_items})
    r1 = await mgr.get_feed(session_id="u", limit=10)
    r2 = await mgr.get_feed(session_id="u", limit=10, cursor=r1.next_page)
    ids1 = [it["id"] for it in r1.data]
    ids2 = [it["id"] for it in r2.data]
    assert ids2 and ids2 != ids1  # page 2 advanced both sources


@pytest.mark.asyncio
async def test_legacy_version_key_is_ignored():
    """Configs carrying the removed top-level `version` tag still parse."""
    mgr = FeedManager(config={"version": "2", **CONFIG}, methods_dict={"items": make_items})
    r = await mgr.get_feed(session_id="u", limit=4)
    assert len(r.data) == 4
