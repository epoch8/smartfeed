"""BUG #6 -- a soft-failed subfeed returns the whole inbound cursor.

On raise_error=False, SubFeed.execute returns ``next_page=cursor`` -- the entire
feed cursor -- instead of ``{subfeed_id: ...}`` (subfeed.py:39). _merge_cursor
(mixers.py:12-16) does ``merged.update(c)`` per child in child order, so a failed
child that sorts AFTER a healthy sibling dumps all inbound keys and overwrites
the sibling's fresh cursor with the stale inbound value.

Fails today; passes once the failure path returns a cursor scoped to its own id.
"""

import pytest

from tests import sources as S
from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from smartfeed.models.mixers import MergerAppend
from smartfeed.execution import executor as run_executor


async def good_source(user_id, limit, next_page, **kw):
    pos = next_page.get("pos", 0)
    data = [{"id": pos + i} for i in range(limit)]
    return FeedResult(data=data, next_page={"pos": pos + limit}, has_next_page=True)


async def failing_source(user_id, limit, next_page, **kw):
    raise RuntimeError("upstream down")


METHODS = {"good": good_source, "bad": failing_source}


def _node():
    # child order [good, bad] so the failed child's cursor merges LAST.
    return MergerAppend(node_id="top", items=[
        SubFeed(subfeed_id="good_src", method_name="good"),
        SubFeed(subfeed_id="bad_src", method_name="bad", raise_error=False),
    ])


@pytest.mark.asyncio
async def test_softfail_does_not_clobber_sibling_cursor():
    ctx = S.make_ctx(METHODS, redis=None)
    # Inbound cursor carries a STALE position for good_src.
    inbound = {"good_src": {"pos": 5}}
    r = await run_executor.run(_node(), ctx, limit=10, cursor=inbound)
    # good_src is asked for demand 5 (MergerAppend split of limit 10) from pos 5,
    # so its fresh cursor is pos 10. The failed sibling must not overwrite it.
    assert r.next_page.get("good_src", {}).get("pos") == 10, (
        f"good_src cursor was clobbered by the failed sibling: {r.next_page}"
    )


@pytest.mark.asyncio
async def test_softfail_cursor_is_scoped_to_own_id():
    ctx = S.make_ctx(METHODS, redis=None)
    inbound = {"good_src": {"pos": 5}, "unrelated": {"x": 1}}
    r = await run_executor.run(_node(), ctx, limit=10, cursor=inbound)
    # The failed subfeed must not resurrect unrelated inbound cursor keys.
    assert "unrelated" not in r.next_page, (
        f"failed subfeed leaked unrelated inbound cursor keys: {r.next_page}"
    )


@pytest.mark.asyncio
async def test_softfail_clobber_reserves_items_downstream():
    """The user-facing consequence: because the failed sibling rewinds good_src's
    cursor, a subsequent page re-serves items already shown."""
    ctx = S.make_ctx(METHODS, redis=None)
    r1 = await run_executor.run(_node(), ctx, limit=10, cursor={})
    r2 = await run_executor.run(_node(), ctx, limit=10, cursor=r1.next_page)
    r3 = await run_executor.run(_node(), ctx, limit=10, cursor=r2.next_page)
    ids = [it["id"] for pg in (r1.data, r2.data, r3.data) for it in pg]
    dupes = sorted({x for x in ids if ids.count(x) > 1})
    assert not dupes, f"items re-served across pages due to cursor clobber: {dupes}"
