from typing import Any

import pytest

from smartfeed.feed_models import _redis_call
from smartfeed.policies.dedup_utils import decode_seen_from_cursor, encode_seen_for_cursor, redis_zmscore
from tests.fixtures.redis import redis_client  # noqa: F401


class _RedisNoZmscore:
    """Wrapper around a real sync redis client to force the pipeline fallback.

    This keeps the backend "real Redis" while exercising the no-zmscore branch.
    """

    zmscore = None  # type: ignore[assignment]

    def __init__(self, client: Any) -> None:
        self._client = client

    def pipeline(self) -> Any:
        return self._client.pipeline()


def test_encode_decode_seen_cursor_compressed_roundtrip_and_truncation() -> None:
    seen_updates = [("a", 1), ("b", 2), ("c", 3)]

    encoded = encode_seen_for_cursor(seen_updates, cursor_compress=True, cursor_max_keys=None)
    decoded = decode_seen_from_cursor(encoded)
    assert decoded == {"a": 1, "b": 2, "c": 3}

    encoded_trunc = encode_seen_for_cursor(seen_updates, cursor_compress=True, cursor_max_keys=2)
    decoded_trunc = decode_seen_from_cursor(encoded_trunc)
    assert decoded_trunc == {"b": 2, "c": 3}


def test_decode_seen_cursor_v2_only() -> None:
    assert decode_seen_from_cursor(None) == {}

    # Supported v2 uncompressed format
    assert decode_seen_from_cursor({"v": 2, "seen": [["a", 9], ["b", 1]]}) == {"a": 9, "b": 1}

    # Legacy/unknown shapes are intentionally rejected
    assert decode_seen_from_cursor(["x", "y"]) == {}
    assert decode_seen_from_cursor({"a": 1}) == {}
    assert decode_seen_from_cursor({"v": 1, "seen": [["a", 1]]}) == {}


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_redis_zmscore_native(redis_client) -> None:
    key = "test_zmscore_native"
    await _redis_call(redis_client, "delete", key)
    await _redis_call(redis_client, "zadd", key, mapping={"a": 1.0, "b": 2.0})

    res = await redis_zmscore(redis_client, key, ["a", "missing", "b"])
    assert res == [1.0, None, 2.0]

    await _redis_call(redis_client, "delete", key)


@pytest.mark.parametrize("redis_client", ["sync"], indirect=True)
@pytest.mark.asyncio
async def test_redis_zmscore_pipeline_fallback_for_sync_client_without_zmscore(redis_client) -> None:
    key = "test_zmscore_fallback"
    await _redis_call(redis_client, "delete", key)
    await _redis_call(redis_client, "zadd", key, mapping={"a": 1.0, "b": 2.0})

    wrapped = _RedisNoZmscore(redis_client)
    res = await redis_zmscore(wrapped, key, ["a", "missing", "b"])  # type: ignore[arg-type]
    assert res == [1.0, None, 2.0]

    await _redis_call(redis_client, "delete", key)
