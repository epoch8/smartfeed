"""The gradient demand computation was rewritten from an O(limit * page) replay
of the whole shift history into an O(limit / size_to_step) walk over the current
page window with a closed-form percentage (a tampered or
legitimately deep `page` must not grow per-request work).

This file pins (a) exact output equivalence with the original algorithm, kept
verbatim below as the reference, and (b) the bounded-work property.
"""

from typing import Dict, List

from smartfeed.models.mixers import MergerPercentageGradient, MergerPercentageItem
from smartfeed.models.subfeed import SubFeed


def _reference_demands(pct_from: int, pct_to: int, step: int, size_to_step: int, page: int, limit: int) -> tuple:
    """The pre-rewrite algorithm, verbatim -- the behavioral reference."""
    percentage_from = pct_from
    percentage_to = pct_to
    start_position = limit * (page - 1)
    first_iter = True
    limit_from = 0
    limit_to = 0
    segments: List[Dict] = []

    for i in range(size_to_step, limit * page + size_to_step, size_to_step):
        if not first_iter and percentage_to < 100:
            percentage_from -= step
            percentage_to += step
            if percentage_to > 100 or percentage_from < 0:
                percentage_from = 0
                percentage_to = 100

        if i > start_position:
            iter_limit = (limit * page - start_position) if i > limit * page else (i - start_position)
            start_position = i
            from_take = iter_limit * percentage_from // 100
            to_take = iter_limit - from_take
            limit_from += from_take
            limit_to += to_take
            segments.append({"limit": iter_limit, "from_take": from_take, "to_take": to_take})

        if first_iter:
            first_iter = False

    return limit_from, limit_to, segments


def _node(pct_from: int, pct_to: int, step: int, size_to_step: int) -> MergerPercentageGradient:
    return MergerPercentageGradient(
        node_id="g",
        item_from=MergerPercentageItem(percentage=pct_from, data=SubFeed(subfeed_id="a", method_name="a")),
        item_to=MergerPercentageItem(percentage=pct_to, data=SubFeed(subfeed_id="b", method_name="b")),
        step=step,
        size_to_step=size_to_step,
    )


def test_window_rewrite_matches_reference_exactly():
    cases = 0
    for pct_from, pct_to in [(80, 20), (100, 0), (0, 100), (50, 70), (60, 20), (90, 30), (50, 50), (70, 0)]:
        for step in (1, 3, 10, 25, 40, 100):
            for size_to_step in (1, 2, 3, 7, 10, 30):
                node = _node(pct_from, pct_to, step, size_to_step)
                for limit in (1, 3, 5, 10, 13):
                    for page in (1, 2, 3, 5, 8):
                        got = node._calculate_demands(page, limit)
                        want = _reference_demands(pct_from, pct_to, step, size_to_step, page, limit)
                        assert got == want, (
                            f"mismatch at from={pct_from} to={pct_to} step={step} "
                            f"size_to_step={size_to_step} page={page} limit={limit}: {got} != {want}"
                        )
                        cases += 1
    assert cases == 8 * 6 * 6 * 5 * 5


def test_deep_page_work_is_bounded_by_page_size():
    node = _node(80, 20, 10, 10)
    limit = 20
    limit_from, limit_to, segments = node._calculate_demands(page=10**6, limit=limit)
    # Work is bounded by the page window, not the scroll depth.
    assert len(segments) <= limit // 10 + 2
    # By page 10**6 the gradient long since clamped to (0, 100).
    assert (limit_from, limit_to) == (0, limit)


def test_page_window_is_fully_allocated():
    """from_take + to_take always sum to the page window, whatever the params."""
    node = _node(80, 20, 7, 9)
    for page in (1, 2, 4, 100):
        limit_from, limit_to, segments = node._calculate_demands(page, 10)
        assert limit_from + limit_to == 10
        assert sum(s["limit"] for s in segments) == 10
