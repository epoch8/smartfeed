"""Positional subfeed with highest dedup_priority must NOT be refilled
when it returns fewer items than requested slots.

Reproduces production bug: TopSort (promo) returns fewer ads than
positional slots → _refill_deficits hardcodes has_next_page=True
in initial state → pointless refill calls even though the subfeed
already signalled has_next_page=False.
"""

import pytest

from smartfeed.schemas import FeedResultNextPage, MergerDeduplication
from tests.fixtures import dedup_helpers as dh
from tests.utils import parse_model


def _make_counting_method(items):
    """Offset-paged method that also counts how many times it was called."""
    call_count = {"value": 0}

    async def _method(user_id, limit, next_page, **kwargs):
        call_count["value"] += 1
        from smartfeed.schemas import FeedResultClient

        offset = int(next_page.after or 0)
        result_data = items[offset : offset + limit]
        next_page.after = offset + len(result_data)
        next_page.page += 1
        has_next_page = (offset + len(result_data)) < len(items)
        return FeedResultClient(data=result_data, next_page=next_page, has_next_page=has_next_page)

    return _method, call_count


@pytest.mark.asyncio
async def test_positional_high_priority_no_refill_when_exhausted():
    """When the positional subfeed (dedup_priority=1, highest) returns fewer
    items than requested AND has_next_page=False, it must NOT be refilled.

    Setup:
    - positional (promo): only 2 items, dedup_priority=1
    - default: 100 items, dedup_priority=0
    - positions=[1,3,5,7] → needs 4 promo items
    - promo returns 2 out of 4 → has_next_page=False
    - Expected: promo called exactly ONCE (no refill)
    """
    promo_items = dh.make_items("promo", 1001, 1003)  # only 2 items
    default_items = dh.make_items("default", 1, 101)  # 100 items, no overlap

    promo_method, promo_calls = _make_counting_method(promo_items)
    default_method, default_calls = _make_counting_method(default_items)

    methods_dict = {
        "promo": promo_method,
        "default": default_method,
    }

    config = dh._dedup_config(
        "dedup_wrapper",
        dh._positional_config(
            "pos_mix",
            positions=[1, 3, 5, 7],
            positional=dh._subfeed("sf_promo", "promo", dedup_priority=1),
            default=dh._subfeed("sf_default", "default", dedup_priority=0),
        ),
        max_refill_loops=5,
        overfetch_factor=1,
    )

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) > 0

    # The critical assertion: promo must be called only once.
    # Before the fix, it was called 1 + max_refill_loops times
    # because _refill_deficits hardcoded has_next_page=True.
    assert promo_calls["value"] == 1, (
        f"Promo was called {promo_calls['value']} times, expected 1. "
        f"Refill should not retry a subfeed that returned has_next_page=False."
    )


@pytest.mark.asyncio
async def test_positional_high_priority_no_refill_even_with_overfetch():
    """Same as above but with overfetch_factor > 1 to ensure the fix
    works regardless of overfetch settings."""
    promo_items = dh.make_items("promo", 1001, 1003)  # only 2 items
    default_items = dh.make_items("default", 1, 101)

    promo_method, promo_calls = _make_counting_method(promo_items)
    default_method, _ = _make_counting_method(default_items)

    methods_dict = {
        "promo": promo_method,
        "default": default_method,
    }

    config = dh._dedup_config(
        "dedup_wrapper",
        dh._positional_config(
            "pos_mix",
            positions=[1, 3, 5, 7],
            positional=dh._subfeed("sf_promo", "promo", dedup_priority=1),
            default=dh._subfeed("sf_default", "default", dedup_priority=0),
        ),
        max_refill_loops=5,
        overfetch_factor=5,
    )

    merger = parse_model(MergerDeduplication, config)
    res = await merger.get_data(
        methods_dict=methods_dict,
        user_id="u",
        limit=20,
        next_page=FeedResultNextPage(data={}),
    )

    assert len(res.data) > 0
    assert promo_calls["value"] == 1, (
        f"Promo was called {promo_calls['value']} times with overfetch_factor=5. "
        f"Expected 1 — exhausted subfeed must not be refilled."
    )
