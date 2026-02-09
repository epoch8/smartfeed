from typing import Any

import pytest

from smartfeed.feed_models import _redis_call
from smartfeed.policies.dedup_utils import decode_seen_from_cursor
from smartfeed.policies.seen_store import CursorSeenStore, RedisSeenStore
from tests.fixtures.redis import redis_client


@pytest.mark.asyncio
async def test_cursor_seen_store_set_max_and_commit_roundtrip() -> None:
    store = CursorSeenStore.from_after(after=None, cursor_compress=True, cursor_max_keys=None)
    store.set_max("a", 1)
    store.set_max("a", 1)  # no-op
    store.set_max("a", 0)  # no-op (lower)
    store.set_max("b", 2)

    after = await store.commit()
    decoded = decode_seen_from_cursor(after)
    assert decoded == {"a": 1, "b": 2}


@pytest.mark.asyncio
async def test_cursor_seen_store_commit_keeps_previous_cursor_state() -> None:
    store = CursorSeenStore.from_after(
        after={"v": 2, "seen": [["a", 1], ["b", 2]]},
        cursor_compress=False,
        cursor_max_keys=None,
    )
    store.set_max("c", 3)

    after = await store.commit()
    decoded = decode_seen_from_cursor(after)
    assert decoded == {"a": 1, "b": 2, "c": 3}


@pytest.mark.parametrize("redis_client", ["sync", "async"], indirect=True)
@pytest.mark.asyncio
async def test_redis_seen_store_prefetch_set_max_commit_and_reset(redis_client) -> None:
    key = "test_seen_store"
    await _redis_call(redis_client, "delete", key)
    # Pre-seed zset state
    await _redis_call(redis_client, "zadd", key, mapping={"a": 5.0})

    store = RedisSeenStore.create(redis_client=redis_client, redis_key=key, ttl_seconds=60)

    await store.prefetch(["a", "a", "b"])  # duplicates
    assert store.get("a") == 5
    assert store.get("b") is None

    store.set_max("a", 3)  # should not reduce existing
    store.set_max("b", 2)

    await store.commit()

    # New state should be present in redis
    scores = list(await _redis_call(redis_client, "zmscore", key, ["a", "b"]))
    assert scores == [5.0, 2.0]

    await store.reset()
    scores_after_reset = list(await _redis_call(redis_client, "zmscore", key, ["a", "b"]))
    assert scores_after_reset == [None, None]

    await _redis_call(redis_client, "delete", key)
