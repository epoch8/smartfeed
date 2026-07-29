"""README's documented "sharp edges" pinned as tests.

Edge 2 (rerank length mismatch always raises) and edges 1/3/4 previously had no
coverage in the exact configurations the README warns about. Edge 5 (cursor
validation) lives in test_input_validation.py.
"""

import pytest

from smartfeed.models.base import FeedResult
from smartfeed.models.subfeed import SubFeed
from tests import sources as S
from smartfeed.execution import executor as run_executor


# -- edge 1: missing method_name ----------------------------------------------


@pytest.mark.asyncio
async def test_missing_method_raises_keyerror_even_fail_soft():
    """method_name is resolved BEFORE the try block: KeyError regardless of raise_error."""
    node = SubFeed(subfeed_id="s", method_name="no_such_method", raise_error=False)
    ctx = S.make_ctx({})
    with pytest.raises(KeyError, match="no_such_method"):
        await run_executor.run(node, ctx, limit=5, cursor={})


# -- edge 3: fetch function without **kwargs -----------------------------------


async def no_kwargs_fetch(user_id, limit, next_page):
    return FeedResult(data=[{"id": 1}], next_page={}, has_next_page=False)


@pytest.mark.asyncio
async def test_no_kwargs_source_raises_typeerror():
    node = S.subfeed("s", "src")
    ctx = S.make_ctx({"src": no_kwargs_fetch})
    with pytest.raises(TypeError):
        await run_executor.run(node, ctx, limit=5, cursor={})


@pytest.mark.asyncio
async def test_no_kwargs_source_swallowed_to_empty_page_when_fail_soft():
    """The documented footgun: under raise_error=False the signature bug reads
    exactly like 'this source had no items today'."""
    node = SubFeed(subfeed_id="s", method_name="src", raise_error=False)
    ctx = S.make_ctx({"src": no_kwargs_fetch})
    r = await run_executor.run(node, ctx, limit=5, cursor={})
    assert r.data == []
    assert r.has_next_page is False


# -- edge 4: subfeed_params colliding with injected kwargs ----------------------


@pytest.mark.asyncio
async def test_subfeed_params_collision_raises_typeerror():
    async def fine_fetch(user_id, limit, next_page, **kw):
        return FeedResult(data=[], next_page={}, has_next_page=False)

    node = SubFeed(subfeed_id="s", method_name="src", subfeed_params={"limit": 3})
    ctx = S.make_ctx({"src": fine_fetch})
    with pytest.raises(TypeError):
        await run_executor.run(node, ctx, limit=5, cursor={})


# -- edge 2: rerank length mismatch always raises -------------------------------


@pytest.mark.asyncio
async def test_rerank_length_mismatch_raises_even_fail_soft():
    """rerank.raise_error=False covers only exceptions FROM the callable; the
    length contract is enforced unconditionally."""

    async def drops_one(items, session_id):
        return items[:-1]

    node = S.wrapper(S.subfeed("s", "src"), rerank_method="drops_one", rerank_raise=False)
    ctx = S.make_ctx({"src": S.ScriptedSource(S.unique_pool(10)), "drops_one": drops_one})
    with pytest.raises(ValueError, match="must return exactly"):
        await run_executor.run(node, ctx, limit=5, cursor={})
