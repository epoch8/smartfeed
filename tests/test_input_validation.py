"""Input validation at the public boundary and on untrusted cursor contents.

Policy: integrator-level type errors (limit, session_id, cursor type) AND
tampered per-node cursor contents raise loudly (ValueError/TypeError) instead
of crashing mid-slice, serving wrong pages, or silently degrading.
"""

import decimal

import pytest
import fakeredis.aioredis

from smartfeed.manager import FeedManager
from smartfeed.models.base import FeedResult
from smartfeed.models.mixers import MergerPercentageGradient, MergerPercentageItem
from tests import sources as S
from smartfeed.execution import executor as run_executor


CONFIG = {"version": "2", "feed": {"type": "subfeed", "subfeed_id": "src", "method_name": "src"}}


async def _one_item(user_id, limit, next_page, **kw):
    return FeedResult(data=[{"id": 1}], next_page={}, has_next_page=False)


def _manager():
    return FeedManager(config=CONFIG, methods_dict={"src": _one_item}, redis_client=None)


# -- get_feed boundary (#14) -------------------------------------------------


@pytest.mark.asyncio
async def test_limit_zero_and_negative_raise():
    mgr = _manager()
    for bad in (0, -3):
        with pytest.raises(ValueError, match="limit"):
            await mgr.get_feed(session_id="u", limit=bad)


@pytest.mark.asyncio
async def test_limit_non_int_raises():
    with pytest.raises(ValueError, match="limit"):
        # Deliberately ill-typed: proves get_feed rejects a non-int limit at runtime.
        await _manager().get_feed(session_id="u", limit="10")  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_cursor_wrong_type_raises():
    with pytest.raises(TypeError, match="cursor"):
        # Deliberately ill-typed: proves get_feed rejects a non-dict cursor at runtime.
        await _manager().get_feed(session_id="u", limit=10, cursor="abc")  # pyright: ignore[reportArgumentType]


@pytest.mark.asyncio
async def test_empty_session_id_raises():
    with pytest.raises(ValueError, match="session_id"):
        await _manager().get_feed(session_id="", limit=10)


# -- cached-wrapper cursor tampering (#13) ------------------------------------


def _cached_wrapper_ctx():
    src = S.ScriptedSource(S.unique_pool(30))
    node = S.wrapper(S.subfeed("src", "src"), session_size=10)
    ctx = S.make_ctx({"src": src}, redis=fakeredis.aioredis.FakeRedis())
    return node, ctx


@pytest.mark.asyncio
async def test_tampered_offset_rejected():
    node, ctx = _cached_wrapper_ctx()
    r1 = await run_executor.run(node, ctx, limit=5, cursor={})
    gen = r1.next_page["w"]["gen"]
    for bad_offset in (-4, "5", 2.5, None):
        with pytest.raises(ValueError, match="Wrapper 'w'.*offset"):
            await run_executor.run(node, ctx, limit=5, cursor={"w": {"offset": bad_offset, "gen": gen}})


@pytest.mark.asyncio
async def test_tampered_gen_type_rejected():
    node, ctx = _cached_wrapper_ctx()
    await run_executor.run(node, ctx, limit=5, cursor={})
    with pytest.raises(ValueError, match="Wrapper 'w'.*gen"):
        await run_executor.run(node, ctx, limit=5, cursor={"w": {"offset": 5, "gen": 123}})


@pytest.mark.asyncio
async def test_node_cursor_entry_not_dict_rejected():
    node, ctx = _cached_wrapper_ctx()
    with pytest.raises(ValueError, match="Wrapper 'w'.*must be a dict"):
        await run_executor.run(node, ctx, limit=5, cursor={"w": "junk"})


@pytest.mark.asyncio
async def test_huge_offset_is_valid_and_triggers_continuation():
    """A huge-but-valid int offset is not tampering: it means 'past the batch',
    which is the documented cache-exhausted continuation path."""
    node, ctx = _cached_wrapper_ctx()
    r1 = await run_executor.run(node, ctx, limit=5, cursor={})
    gen = r1.next_page["w"]["gen"]
    r = await run_executor.run(node, ctx, limit=5, cursor={"w": {"offset": 10**9, "gen": gen}})
    assert len(r.data) == 5  # continuation rebuild served a full fresh page


@pytest.mark.asyncio
async def test_subfeed_cursor_slice_not_dict_rejected():
    node = S.subfeed("src", "src")
    ctx = S.make_ctx({"src": S.ScriptedSource(S.unique_pool(5))})
    with pytest.raises(ValueError, match="SubFeed 'src'"):
        await run_executor.run(node, ctx, limit=5, cursor={"src": "junk"})


# -- gradient cursor tampering (#13/#16) ---------------------------------------


@pytest.mark.asyncio
async def test_gradient_tampered_page_rejected():
    node = MergerPercentageGradient(
        node_id="grad",
        item_from=MergerPercentageItem(percentage=80, data=S.subfeed("a", "a")),
        item_to=MergerPercentageItem(percentage=20, data=S.subfeed("b", "b")),
        step=10,
        size_to_step=10,
    )
    ctx = S.make_ctx({"a": S.ScriptedSource(S.unique_pool(50)), "b": S.ScriptedSource(S.unique_pool(50, start=100))})
    for bad_cursor in ({"grad": {"page": 0}}, {"grad": {"page": -1}}, {"grad": {"page": "2"}}, {"grad": "junk"}):
        with pytest.raises(ValueError, match="grad"):
            await run_executor.run(node, ctx, limit=10, cursor=bad_cursor)


# -- cache serialization asymmetry (#17) ---------------------------------------


async def _decimal_source(user_id, limit, next_page, **kw):
    return FeedResult(data=[{"id": 1, "price": decimal.Decimal("9.99")}], next_page={}, has_next_page=False)


@pytest.mark.asyncio
async def test_non_serializable_item_with_cache_raises_named_error():
    node = S.wrapper(S.subfeed("src", "src"), session_size=10)
    ctx = S.make_ctx({"src": _decimal_source}, redis=fakeredis.aioredis.FakeRedis())
    with pytest.raises(TypeError, match="Wrapper 'w'.*orjson-serializable"):
        await run_executor.run(node, ctx, limit=5, cursor={})


@pytest.mark.asyncio
async def test_non_serializable_item_passthrough_works_control():
    """Control: the same item is fine without cache -- the asymmetry is documented,
    and the cached path's error above must name the node so it is diagnosable."""
    node = S.wrapper(S.subfeed("src", "src"))
    ctx = S.make_ctx({"src": _decimal_source}, redis=None)
    r = await run_executor.run(node, ctx, limit=5, cursor={})
    assert len(r.data) == 1
