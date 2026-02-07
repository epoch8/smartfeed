from __future__ import annotations

import asyncio
import base64
import zlib
from typing import Any, Awaitable, Dict, List, Optional, Tuple, Union, cast

import redis
from redis.asyncio import Redis as AsyncRedis

from .. import jsonlib as json
from ..feed_models import _is_async_redis_client, _redis_call


def decode_seen_from_cursor(after: Any) -> Dict[str, int]:
    if after is None:
        return {}

    if isinstance(after, dict) and "z" in after:
        payload = base64.urlsafe_b64decode(str(after["z"]).encode())
        raw = zlib.decompress(payload).decode()
        decoded = json.loads(raw)
        if isinstance(decoded, dict):
            return {str(k): int(v) for k, v in decoded.items()}
        if isinstance(decoded, list):
            seen_map: Dict[str, int] = {}
            for entry_item in decoded:
                if isinstance(entry_item, (list, tuple)) and len(entry_item) == 2:
                    seen_map[str(entry_item[0])] = int(entry_item[1])
                else:
                    seen_map[str(entry_item)] = 0
            return seen_map
        return {}

    if isinstance(after, dict) and "seen" in after:
        return {str(k): 0 for k in list(after["seen"])}
    if isinstance(after, list):
        return {str(k): 0 for k in list(after)}
    if isinstance(after, dict):
        return {str(k): int(v) for k, v in after.items() if k not in {"v", "c", "n"}}
    return {}


def encode_seen_for_cursor(
    seen_updates_in_order: List[Tuple[str, int]],
    *,
    cursor_compress: bool,
    cursor_max_keys: Optional[int],
) -> Any:
    if cursor_max_keys is not None:
        seen_updates_in_order = seen_updates_in_order[-cursor_max_keys:]

    if not cursor_compress:
        return {"v": 2, "seen": [[k, p] for k, p in seen_updates_in_order]}

    raw = json.dumps([[k, p] for k, p in seen_updates_in_order]).encode()
    compressed = zlib.compress(raw)
    return {
        "v": 2,
        "c": "zlib+base64",
        "n": len(seen_updates_in_order),
        "z": base64.urlsafe_b64encode(compressed).decode(),
    }


async def redis_zmscore(
    redis_client: Union[redis.Redis, AsyncRedis],
    key: str,
    members: List[str],
) -> List[Optional[float]]:
    if not members:
        return []

    if getattr(redis_client, "zmscore", None) is not None:
        res = await _redis_call(redis_client, "zmscore", key, members)
        return [None if v is None else float(v) for v in list(res)]

    if not _is_async_redis_client(redis_client):

        def _sync_pipeline_execute() -> Any:
            pipe = redis_client.pipeline()
            for m in members:
                pipe.zscore(key, m)
            return pipe.execute()

        res = await asyncio.to_thread(_sync_pipeline_execute)
        return [None if v is None else float(v) for v in list(res)]

    pipe = redis_client.pipeline()
    for m in members:
        pipe.zscore(key, m)
    res = await cast(Awaitable[Any], pipe.execute())
    return [None if v is None else float(v) for v in list(res)]


async def redis_zadd_and_expire(
    redis_client: Union[redis.Redis, AsyncRedis],
    key: str,
    member_scores: Dict[str, int],
    *,
    ttl_seconds: int,
) -> None:
    if not member_scores:
        return
    await _redis_call(redis_client, "zadd", key, mapping={m: float(s) for m, s in member_scores.items()})
    await _redis_call(redis_client, "expire", key, ttl_seconds)
