from __future__ import annotations

import asyncio
import base64
import inspect
import zlib
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, Iterator, List, Literal, Optional, Union, cast

import redis
from pydantic import PrivateAttr, model_validator
from redis.asyncio import Redis as AsyncRedis

from .. import jsonlib as json
from ..feed_models import (
    BaseFeedConfigModel,
    FeedResult,
    FeedResultClient,
    FeedResultNextPage,
    FeedResultNextPageInside,
    SubFeed,
    _is_async_redis_client,
    _pydantic_deep_copy,
    _redis_call,
)

if TYPE_CHECKING:
    from ..schemas import FeedTypes


class _DedupState(ABC):
    @abstractmethod
    def should_accept(self, key: str, priority: int) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record(self, key: str, priority: int) -> None:
        raise NotImplementedError

    async def prefetch(self, keys: List[str]) -> None:
        return


@dataclass
class _CursorDedupState(_DedupState):
    seen_priority_map: Dict[str, int]
    seen_updates_in_order: List[tuple[str, int]]
    seen_request_set: set[str]

    def should_accept(self, key: str, priority: int) -> bool:
        if key in self.seen_request_set:
            return False
        existing_priority = self.seen_priority_map.get(key)
        if existing_priority is not None and priority <= existing_priority:
            return False
        return True

    def record(self, key: str, priority: int) -> None:
        self.seen_priority_map[key] = priority
        self.seen_updates_in_order.append((key, priority))
        self.seen_request_set.add(key)


@dataclass
class _RedisDedupState(_DedupState):
    redis_client: Union[redis.Redis, AsyncRedis]
    redis_state_key: str
    redis_seen_cache: Dict[str, Optional[int]]
    redis_new_scores: Dict[str, int]
    seen_request_set: set[str]
    zmscore: Callable[
        [Union[redis.Redis, AsyncRedis], str, List[str]],
        Union[Awaitable[List[Optional[float]]], List[Optional[float]]],
    ]

    async def prefetch(self, keys: List[str]) -> None:
        if not keys:
            return
        unique: List[str] = []
        seen: set[str] = set()
        for k in keys:
            if k in self.seen_request_set:
                continue
            if k in self.redis_seen_cache:
                continue
            if k in seen:
                continue
            seen.add(k)
            unique.append(k)

        if not unique:
            return

        scores_result = self.zmscore(self.redis_client, self.redis_state_key, unique)
        if inspect.iscoroutine(scores_result):
            scores = await cast(Awaitable[List[Optional[float]]], scores_result)
        else:
            scores = cast(List[Optional[float]], scores_result)

        for k, s in zip(unique, scores):
            self.redis_seen_cache[k] = None if s is None else int(s)

    def should_accept(self, key: str, priority: int) -> bool:
        if key in self.seen_request_set:
            return False
        existing_priority = self.redis_seen_cache.get(key)
        if existing_priority is not None and priority <= existing_priority:
            return False
        return True

    def record(self, key: str, priority: int) -> None:
        self.seen_request_set.add(key)
        self.redis_seen_cache[key] = priority
        self.redis_new_scores[key] = max(self.redis_new_scores.get(key, 0), priority)


class MergerDeduplication(BaseFeedConfigModel):
    """Merger that deduplicates while preserving child mixing/position semantics."""

    merger_id: str
    type: Literal["merger_deduplication"]
    data: "FeedTypes"

    dedup_key: Optional[str] = None
    missing_key_policy: Literal["error", "keep", "drop"] = "error"

    state_backend: Literal["cursor", "redis"] = "cursor"
    state_ttl_seconds: int = 3600
    cursor_compress: bool = True
    cursor_max_keys: Optional[int] = None

    overfetch_factor: int = 1

    max_refill_loops: int = 20

    _descendant_cursor_keys_cache: Optional[set[str]] = PrivateAttr(default=None)

    @model_validator(mode="after")
    def validate_merger_deduplication(self) -> "MergerDeduplication":
        if self.overfetch_factor < 1:
            raise ValueError('"overfetch_factor" must be >= 1')
        if self.max_refill_loops < 1:
            raise ValueError('"max_refill_loops" must be >= 1')
        return self

    def _collect_descendant_cursor_keys(self, feed: BaseFeedConfigModel) -> set[str]:
        keys: set[str] = set()

        subfeed_id = getattr(feed, "subfeed_id", None)
        if isinstance(subfeed_id, str) and subfeed_id:
            keys.add(subfeed_id)

        merger_id = getattr(feed, "merger_id", None)
        if isinstance(merger_id, str) and merger_id:
            keys.add(merger_id)

        child: Any
        for attr_name in ("data", "positional", "default"):
            child = getattr(feed, attr_name, None)
            if isinstance(child, BaseFeedConfigModel):
                keys.update(self._collect_descendant_cursor_keys(child))

        for attr_name in ("item_from", "item_to"):
            child = getattr(feed, attr_name, None)
            inner = getattr(child, "data", None)
            if isinstance(inner, BaseFeedConfigModel):
                keys.update(self._collect_descendant_cursor_keys(inner))

        items = getattr(feed, "items", None)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, BaseFeedConfigModel):
                    keys.update(self._collect_descendant_cursor_keys(item))
                    continue

                inner = getattr(item, "data", None)
                if isinstance(inner, BaseFeedConfigModel):
                    keys.update(self._collect_descendant_cursor_keys(inner))

        return keys

    def _get_descendant_cursor_keys_cached(self) -> set[str]:
        cached = self._descendant_cursor_keys_cache
        if cached is None:
            cached = self._collect_descendant_cursor_keys(self.data)
            self._descendant_cursor_keys_cache = cached
        return cached

    def _reset_descendant_cursors(self, next_page: FeedResultNextPage) -> None:
        descendant_keys = self._get_descendant_cursor_keys_cached()
        for key in descendant_keys:
            next_page.data.pop(key, None)

    def _normalize_key(self, value: Any) -> str:
        if isinstance(value, (str, int)):
            return str(value)
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, default=str)
        return str(value)

    def _extract_dedup_value(self, item: Any) -> Any:
        if not self.dedup_key:
            return item

        try:
            value = item.get(self.dedup_key)
        except AttributeError:
            value = getattr(item, self.dedup_key, None)

        if value is None and self.missing_key_policy == "error":
            raise AssertionError(f"Deduplication failed: entity {item} has no key or attr {self.dedup_key}")
        return value

    def _get_entity_key(self, entity: Any) -> Optional[str]:
        raw_value = self._extract_dedup_value(entity)
        if raw_value is None:
            if self.missing_key_policy == "drop":
                return None
            if self.missing_key_policy == "keep":
                raw_value = ("__missing__", id(entity))
        return self._normalize_key(raw_value)

    def _compute_overfetch_params(self, *, remaining: int, next_after: Any) -> tuple[bool, int, Optional[int]]:
        can_overfetch = isinstance(next_after, int)
        request_limit = max(1, remaining)
        if can_overfetch and self.overfetch_factor > 1:
            request_limit = max(1, remaining * self.overfetch_factor)
        start_after: Optional[int] = int(next_after) if can_overfetch else None
        return can_overfetch, request_limit, start_after

    def _iter_subfeeds(self, feed: BaseFeedConfigModel) -> Iterator[SubFeed]:
        if isinstance(feed, SubFeed):
            yield feed
            return

        for attr_name in ("data", "positional", "default"):
            inner = getattr(feed, attr_name, None)
            if isinstance(inner, BaseFeedConfigModel):
                yield from self._iter_subfeeds(inner)

        for attr_name in ("item_from", "item_to"):
            wrapper = getattr(feed, attr_name, None)
            inner = getattr(wrapper, "data", None)
            if isinstance(inner, BaseFeedConfigModel):
                yield from self._iter_subfeeds(inner)

        items = getattr(feed, "items", None)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, BaseFeedConfigModel):
                    yield from self._iter_subfeeds(item)
                    continue
                inner = getattr(item, "data", None)
                if isinstance(inner, BaseFeedConfigModel):
                    yield from self._iter_subfeeds(inner)

    def _register_wrapped_subfeed_method(
        self,
        *,
        subfeed: SubFeed,
        original_methods_dict: Dict[str, Callable],
        rewritten_methods_dict: Dict[str, Callable],
        dedup_state: _DedupState,
    ) -> None:
        original_name = subfeed.method_name
        original_method = original_methods_dict[original_name]
        unique_name = f"__dedup__{self.merger_id}__{subfeed.subfeed_id}"

        if unique_name in rewritten_methods_dict:
            subfeed.method_name = unique_name
            return

        subfeed.method_name = unique_name
        leaf_priority = int(getattr(subfeed, "dedup_priority", 0))

        wrapped = self._make_wrapped_leaf_method(
            original_method=original_method,
            dedup_state=dedup_state,
            leaf_priority=leaf_priority,
        )
        setattr(wrapped, "_smartfeed_original", original_method)
        rewritten_methods_dict[unique_name] = wrapped

    def _make_wrapped_leaf_method(
        self,
        *,
        original_method: Callable,
        dedup_state: _DedupState,
        leaf_priority: int,
    ) -> Callable:
        async def _wrapped_method(
            user_id: Any,
            limit: int,
            next_page: FeedResultNextPageInside,
            **kw: Any,
        ) -> FeedResultClient:
            collected: List[Any] = []
            upstream_has_next_page = False

            loops = 0
            while len(collected) < limit and loops < self.max_refill_loops:
                loops += 1
                before_len = len(collected)

                remaining = limit - len(collected)
                can_overfetch, request_limit, start_after = self._compute_overfetch_params(
                    remaining=remaining,
                    next_after=next_page.after,
                )

                method_result = await original_method(user_id=user_id, limit=request_limit, next_page=next_page, **kw)
                if not isinstance(method_result, FeedResultClient):
                    raise TypeError('SubFeed function must return "FeedResultClient" instance.')

                upstream_has_next_page = upstream_has_next_page or method_result.has_next_page

                inspected_count = 0

                keys_by_index: Optional[List[Optional[str]]] = None
                if isinstance(dedup_state, _RedisDedupState):
                    keys_by_index = []
                    batch_keys: List[str] = []
                    for entity in method_result.data:
                        key = self._get_entity_key(entity)
                        keys_by_index.append(key)
                        if key is not None:
                            batch_keys.append(key)
                    await dedup_state.prefetch(batch_keys)

                for idx, entity in enumerate(method_result.data, start=1):
                    inspected_count = idx

                    key = keys_by_index[idx - 1] if keys_by_index is not None else self._get_entity_key(entity)
                    if key is None:
                        continue

                    if not dedup_state.should_accept(key, leaf_priority):
                        continue

                    collected.append(entity)
                    dedup_state.record(key, leaf_priority)

                    if len(collected) >= limit:
                        break

                if len(collected) == before_len:
                    if not method_result.has_next_page:
                        break

                if can_overfetch and request_limit > remaining and start_after is not None:
                    end_after = next_page.after
                    if isinstance(end_after, int) and end_after == start_after + len(method_result.data):
                        next_page.after = start_after + inspected_count

            return FeedResultClient(data=collected, next_page=next_page, has_next_page=upstream_has_next_page)

        return _wrapped_method

    def _decode_seen_from_cursor(self, next_page: FeedResultNextPage) -> Dict[str, int]:
        entry = next_page.data.get(self.merger_id)
        if not entry or entry.after is None:
            return {}

        after = entry.after
        if isinstance(after, dict) and "z" in after:
            payload = base64.urlsafe_b64decode(after["z"].encode())
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

    def _encode_seen_for_cursor(self, seen_updates_in_order: List[tuple[str, int]]) -> Any:
        if self.cursor_max_keys is not None:
            seen_updates_in_order = seen_updates_in_order[-self.cursor_max_keys :]

        if not self.cursor_compress:
            return {"v": 2, "seen": [[k, p] for k, p in seen_updates_in_order]}

        raw = json.dumps([[k, p] for k, p in seen_updates_in_order]).encode()
        compressed = zlib.compress(raw)
        return {
            "v": 2,
            "c": "zlib+base64",
            "n": len(seen_updates_in_order),
            "z": base64.urlsafe_b64encode(compressed).decode(),
        }

    async def _redis_zmscore(
        self,
        redis_client: Union[redis.Redis, AsyncRedis],
        key: str,
        members: List[str],
    ) -> List[Optional[float]]:
        if not members:
            return []

        zmscore_fn = getattr(redis_client, "zmscore", None)
        if zmscore_fn is not None:
            res = zmscore_fn(key, members)
            if inspect.iscoroutine(res):
                res = await res
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
        res = pipe.execute()
        if inspect.iscoroutine(res):
            res = await res
        return [None if v is None else float(v) for v in list(res)]

    async def _redis_zadd_and_expire(
        self,
        redis_client: Union[redis.Redis, AsyncRedis],
        key: str,
        member_scores: Dict[str, int],
    ) -> None:
        if not member_scores:
            return
        await _redis_call(redis_client, "zadd", key, mapping={m: float(s) for m, s in member_scores.items()})
        await _redis_call(redis_client, "expire", key, self.state_ttl_seconds)

    def _build_redis_state_key(self, user_id: Any, params: Dict[str, Any]) -> str:
        suffix = params.get("custom_deduplication_key") or params.get("custom_view_session_key")
        if suffix:
            return f"dedup:{self.merger_id}:{user_id}:{suffix}"
        return f"dedup:{self.merger_id}:{user_id}"

    async def get_data(
        self,
        methods_dict: Dict[str, Callable],
        user_id: Any,
        limit: int,
        next_page: FeedResultNextPage,
        redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None,
        **params: Any,
    ) -> FeedResult:
        if limit <= 0:
            return FeedResult(data=[], next_page=next_page, has_next_page=False)

        entry = next_page.data.get(self.merger_id)
        requested_page = entry.page if entry is not None else None
        is_fresh_session = requested_page is None or (isinstance(requested_page, int) and requested_page <= 0)

        if self.state_backend == "redis" and not redis_client:
            raise ValueError("Redis client must be provided if using MergerDeduplication with state_backend=redis")

        working_next_page = _pydantic_deep_copy(next_page)

        if is_fresh_session:
            self._reset_descendant_cursors(working_next_page)

        seen_priority_map: Dict[str, int] = {}
        seen_updates_in_order: List[tuple[str, int]] = []
        if self.state_backend == "cursor" and not is_fresh_session:
            seen_priority_map = self._decode_seen_from_cursor(next_page)

        seen_request_set: set[str] = set(seen_priority_map.keys())

        redis_state_key = ""
        redis_new_scores: Dict[str, int] = {}
        redis_seen_cache: Dict[str, Optional[int]] = {}
        if self.state_backend == "redis" and redis_client:
            redis_state_key = self._build_redis_state_key(user_id=user_id, params=params)
            if is_fresh_session:
                await _redis_call(redis_client, "delete", redis_state_key)

        if self.state_backend == "cursor":
            dedup_state: _DedupState = _CursorDedupState(
                seen_priority_map=seen_priority_map,
                seen_updates_in_order=seen_updates_in_order,
                seen_request_set=seen_request_set,
            )
        else:
            assert redis_client is not None
            dedup_state = _RedisDedupState(
                redis_client=redis_client,
                redis_state_key=redis_state_key,
                redis_seen_cache=redis_seen_cache,
                redis_new_scores=redis_new_scores,
                seen_request_set=seen_request_set,
                zmscore=self._redis_zmscore,
            )

        original_methods_dict = methods_dict

        child = _pydantic_deep_copy(self.data)

        rewritten_methods_dict = dict(original_methods_dict)

        for sf in self._iter_subfeeds(child):
            self._register_wrapped_subfeed_method(
                subfeed=sf,
                original_methods_dict=original_methods_dict,
                rewritten_methods_dict=rewritten_methods_dict,
                dedup_state=dedup_state,
            )

        child_result = await child.get_data(
            methods_dict=rewritten_methods_dict,
            user_id=user_id,
            limit=limit,
            next_page=working_next_page,
            redis_client=redis_client,
            _sf_dedup_active=True,
            **params,
        )

        if self.state_backend == "redis" and redis_client:
            await self._redis_zadd_and_expire(redis_client, redis_state_key, redis_new_scores)

        page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
        merger_after: Any = None
        if self.state_backend == "cursor":
            merger_after = self._encode_seen_for_cursor(seen_updates_in_order)

        result_next_page = _pydantic_deep_copy(child_result.next_page)
        result_next_page.data[self.merger_id] = FeedResultNextPageInside(page=page + 1, after=merger_after)

        return FeedResult(data=child_result.data, next_page=result_next_page, has_next_page=child_result.has_next_page)
