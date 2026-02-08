from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

import redis
from redis.asyncio import Redis as AsyncRedis

from ..feed_models import _redis_call
from .dedup_utils import decode_seen_from_cursor, encode_seen_for_cursor, redis_zadd_and_expire, redis_zmscore


class SeenStore(Protocol):
    async def prefetch(self, keys: List[str]) -> None:
        raise NotImplementedError

    def get(self, key: str) -> Optional[int]:
        raise NotImplementedError

    def set_max(self, key: str, priority: int) -> None:
        raise NotImplementedError

    async def reset(self) -> None:
        raise NotImplementedError

    async def commit(self) -> Any:
        raise NotImplementedError


@dataclass
class CursorSeenStore:
    """Seen-store that persists in the cursor (`after`)."""

    cursor_compress: bool
    cursor_max_keys: Optional[int]

    seen_priority_map: Dict[str, int]
    seen_order: List[str]

    @classmethod
    def from_after(
        cls,
        after: Any,
        *,
        cursor_compress: bool,
        cursor_max_keys: Optional[int],
    ) -> "CursorSeenStore":
        seen_priority_map = decode_seen_from_cursor(after)
        return cls(
            cursor_compress=cursor_compress,
            cursor_max_keys=cursor_max_keys,
            seen_priority_map=seen_priority_map,
            seen_order=list(seen_priority_map.keys()),
        )

    async def prefetch(self, keys: List[str]) -> None:
        return None

    def get(self, key: str) -> Optional[int]:
        return self.seen_priority_map.get(key)

    def set_max(self, key: str, priority: int) -> None:
        existing = self.seen_priority_map.get(key)
        if existing is not None and priority <= existing:
            return
        self.seen_priority_map[key] = priority
        if key in self.seen_order:
            self.seen_order.remove(key)
        self.seen_order.append(key)

    async def reset(self) -> None:
        self.seen_priority_map.clear()
        self.seen_order.clear()

    async def commit(self) -> Any:
        # Persist the full snapshot so dedup state survives beyond 2 pages.
        seen_snapshot_in_order: List[Tuple[str, int]] = [
            (key, self.seen_priority_map[key]) for key in self.seen_order if key in self.seen_priority_map
        ]
        return encode_seen_for_cursor(
            seen_snapshot_in_order,
            cursor_compress=self.cursor_compress,
            cursor_max_keys=self.cursor_max_keys,
        )


@dataclass
class RedisSeenStore:
    """Seen-store backed by a Redis zset (member=key, score=priority)."""

    redis_client: Union[redis.Redis, AsyncRedis]
    redis_key: str
    ttl_seconds: int

    redis_seen_cache: Dict[str, Optional[int]]
    redis_new_scores: Dict[str, int]

    @classmethod
    def create(
        cls,
        *,
        redis_client: Union[redis.Redis, AsyncRedis],
        redis_key: str,
        ttl_seconds: int,
    ) -> "RedisSeenStore":
        return cls(
            redis_client=redis_client,
            redis_key=redis_key,
            ttl_seconds=ttl_seconds,
            redis_seen_cache={},
            redis_new_scores={},
        )

    async def prefetch(self, keys: List[str]) -> None:
        if not keys:
            return

        unique: List[str] = []
        seen: set[str] = set()
        for k in keys:
            if k in self.redis_seen_cache:
                continue
            if k in seen:
                continue
            seen.add(k)
            unique.append(k)

        if not unique:
            return

        scores = await redis_zmscore(self.redis_client, self.redis_key, unique)

        for k, s in zip(unique, scores):
            self.redis_seen_cache[k] = None if s is None else int(s)

    def get(self, key: str) -> Optional[int]:
        return self.redis_seen_cache.get(key)

    def set_max(self, key: str, priority: int) -> None:
        existing = self.redis_seen_cache.get(key)
        if existing is not None and priority <= existing:
            return
        self.redis_seen_cache[key] = priority
        self.redis_new_scores[key] = max(self.redis_new_scores.get(key, 0), priority)

    async def reset(self) -> None:
        await _redis_call(self.redis_client, "delete", self.redis_key)
        self.redis_seen_cache.clear()
        self.redis_new_scores.clear()

    async def commit(self) -> Any:
        await redis_zadd_and_expire(
            self.redis_client,
            self.redis_key,
            self.redis_new_scores,
            ttl_seconds=self.ttl_seconds,
        )
        self.redis_new_scores.clear()
        return None
