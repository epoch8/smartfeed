"""Regression: _collect_priorities skips a nested Wrapper's own
dedup_priority.

The walk's generic BaseModel branch does `getattr(child, "data", child)` to unwrap
MergerPercentageItem-style containers -- but a Wrapper is the one BaseNode that
ALSO has `.data`, so a Wrapper sitting directly in a mixer's `items` list (or as
`positional`/`default`) gets unwrapped into its child and its own dedup_priority
override never propagates.

Documented contract (README "Dedup and dedup_priority"): "a node with a non-zero
dedup_priority overrides its whole subtree" -- for ANY node, wrappers included.

Both sources emit the same id; the copy from the priority-5 subtree must win the
dedup arbitration even though the priority-0 copy is first-seen.
"""

import pytest

from smartfeed.models.base import FeedResult
from smartfeed.models.mixers import MergerAppend, MergerPositional
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.wrapper import Wrapper
from tests import sources as S
from smartfeed.execution import executor as run_executor


def _source(who: str):
    async def fetch(user_id, limit, next_page, **kw):
        return FeedResult(data=[{"id": 1, "who": who}], next_page={}, has_next_page=False)

    return fetch


def _ctx():
    return S.make_ctx({"a": _source("a"), "b": _source("b")})


@pytest.mark.asyncio
async def test_wrapper_in_items_list_priority_wins():
    boosted = Wrapper(node_id="boost", dedup_priority=5, data=SubFeed(subfeed_id="b", method_name="b"))
    merger = MergerAppend(node_id="mix", items=[SubFeed(subfeed_id="a", method_name="a"), boosted])
    node = S.wrapper(merger, dedup_key="id")

    r = await run_executor.run(node, _ctx(), limit=2, cursor={})
    assert len(r.data) == 1
    assert r.data[0]["who"] == "b", (
        f"the priority-5 wrapper subtree must win dedup, got copy from '{r.data[0]['who']}' "
        f"(nested wrapper's dedup_priority was skipped in the priority walk)"
    )


@pytest.mark.asyncio
async def test_wrapper_as_positional_child_priority_wins():
    boosted = Wrapper(node_id="boost", dedup_priority=5, data=SubFeed(subfeed_id="b", method_name="b"))
    merger = MergerPositional(
        node_id="pos",
        positions=[2],
        positional=boosted,
        default=SubFeed(subfeed_id="a", method_name="a"),
    )
    node = S.wrapper(merger, dedup_key="id")

    r = await run_executor.run(node, _ctx(), limit=2, cursor={})
    assert len(r.data) == 1
    assert r.data[0]["who"] == "b", f"the priority-5 wrapper subtree must win dedup, got copy from '{r.data[0]['who']}'"
