"""Dedup edge cases: missing-key policies, non-dict items, key stringification.

Contract/characterization tests. Where behavior is arguably wrong (int-vs-str key
collision) the test documents CURRENT behavior, so a
future change is a conscious decision rather than a silent regression.
"""

import pytest
import fakeredis.aioredis

from tests import sources as S
from smartfeed.models.base import FeedResult


def _redis():
    return fakeredis.aioredis.FakeRedis()


# ---------------------------------------------------------------------------
# missing_key_policy
# ---------------------------------------------------------------------------


async def _missing_key_source(user_id, limit, next_page, **kw):
    # Second item lacks the dedup key "id".
    return FeedResult(
        data=[{"id": 1, "val": "a"}, {"val": "no-id"}, {"id": 2, "val": "b"}],
        next_page={"pos": 0},
        has_next_page=False,
    )


@pytest.mark.asyncio
async def test_missing_key_policy_error_raises():
    src = _missing_key_source
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id", missing_key_policy="error")
    ctx = S.make_ctx({"src": src}, redis=_redis())
    from smartfeed.execution import executor as run_executor

    with pytest.raises(KeyError, match="Dedup key"):
        await run_executor.run(node, ctx, limit=10, cursor={})


@pytest.mark.asyncio
async def test_missing_key_policy_keep_passes_through():
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id", missing_key_policy="keep")
    ctx = S.make_ctx({"src": _missing_key_source}, redis=_redis())
    from smartfeed.execution import executor as run_executor

    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(r.data) == 3  # the no-id item is kept un-deduped


@pytest.mark.asyncio
async def test_missing_key_policy_drop_removes():
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id", missing_key_policy="drop")
    ctx = S.make_ctx({"src": _missing_key_source}, redis=_redis())
    from smartfeed.execution import executor as run_executor

    r = await run_executor.run(node, ctx, limit=10, cursor={})
    ids = [it.get("id") for it in r.data]
    assert ids == [1, 2]  # the no-id item is dropped


# ---------------------------------------------------------------------------
# non-dict items
# ---------------------------------------------------------------------------


async def _non_dict_source(user_id, limit, next_page, **kw):
    return FeedResult(data=[{"id": 1}, "a-string", {"id": 2}, 42], next_page={"pos": 0}, has_next_page=False)


@pytest.mark.asyncio
async def test_non_dict_items_pass_through_dedup():
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id")
    ctx = S.make_ctx({"src": _non_dict_source}, redis=_redis())
    from smartfeed.execution import executor as run_executor

    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert "a-string" in r.data and 42 in r.data  # non-dicts survive


# ---------------------------------------------------------------------------
# key stringification: int 1 vs str "1"
# ---------------------------------------------------------------------------


async def _mixed_type_key_source(user_id, limit, next_page, **kw):
    return FeedResult(
        data=[{"id": 1, "val": "int"}, {"id": "1", "val": "str"}], next_page={"pos": 0}, has_next_page=False
    )


@pytest.mark.asyncio
async def test_int_and_str_key_currently_collide_characterization():
    """CURRENT BEHAVIOR: dedup does str(item[key]), so id=1 (int) and id="1" (str)
    are treated as the SAME key and one is dropped. See review -- decide whether
    mixed-type ids should be distinct. This test pins today's behavior; flip it if
    the product decision is that they must be distinct."""
    node = S.wrapper(S.subfeed("src", "src"), dedup_key="id")
    ctx = S.make_ctx({"src": _mixed_type_key_source}, redis=_redis())
    from smartfeed.execution import executor as run_executor

    r = await run_executor.run(node, ctx, limit=10, cursor={})
    assert len(r.data) == 1, "int 1 and str '1' currently collide via str() coercion"
