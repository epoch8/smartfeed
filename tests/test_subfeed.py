import pytest
from smartfeed.models.subfeed import SubFeed
from tests.conftest import METHODS


@pytest.mark.asyncio
async def test_subfeed_returns_data():
    sf = SubFeed(subfeed_id="items", method_name="items")
    result = await sf.execute(methods_dict=METHODS, session_id="s1", limit=5, cursor={})
    assert len(result.data) == 5
    assert result.has_next_page is True


@pytest.mark.asyncio
async def test_subfeed_stamps_debug_info():
    sf = SubFeed(subfeed_id="my_source", method_name="items")
    result = await sf.execute(methods_dict=METHODS, session_id="s1", limit=3, cursor={})
    for item in result.data:
        info = item["_smartfeed_debug_info"]
        assert info["source"] == "my_source"


@pytest.mark.asyncio
async def test_subfeed_raise_error():
    sf = SubFeed(subfeed_id="err", method_name="error")
    with pytest.raises(RuntimeError, match="subfeed error"):
        await sf.execute(methods_dict=METHODS, session_id="s1", limit=5, cursor={})


@pytest.mark.asyncio
async def test_subfeed_swallow_error():
    sf = SubFeed(subfeed_id="err", method_name="error", raise_error=False)
    result = await sf.execute(methods_dict=METHODS, session_id="s1", limit=5, cursor={})
    assert result.data == []
    assert result.has_next_page is False


@pytest.mark.asyncio
async def test_subfeed_cursor_propagation():
    sf = SubFeed(subfeed_id="items", method_name="items")
    r1 = await sf.execute(methods_dict=METHODS, session_id="s1", limit=5, cursor={})
    r2 = await sf.execute(methods_dict=METHODS, session_id="s1", limit=5, cursor=r1.next_page)
    assert r2.data[0]["id"] == 5  # page 2 starts at id=5
