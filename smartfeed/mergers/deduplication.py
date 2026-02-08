from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

from pydantic import PrivateAttr, model_validator

from ..execution.context import ExecutionContext, RefillExecutionSettings
from ..execution.cursors import CursorMap
from ..execution.executor import CallablePlan
from ..feed_models import (
    BaseFeedConfigModel,
    FeedResult,
    FeedResultNextPage,
    FeedResultNextPageInside,
    _pydantic_deep_copy,
)
from ..policies.dedup import DeduplicationPolicy
from ..policies.seen_store import CursorSeenStore, RedisSeenStore, SeenStore

if TYPE_CHECKING:
    from ..schemas import FeedTypes


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
        stack = [feed]
        while stack:
            node = stack.pop()

            for attr in ("subfeed_id", "merger_id"):
                value = getattr(node, attr, None)
                if isinstance(value, str) and value:
                    keys.add(value)

            for child in (
                getattr(node, "data", None),
                getattr(node, "positional", None),
                getattr(node, "default", None),
            ):
                if isinstance(child, BaseFeedConfigModel):
                    stack.append(child)

            for wrapper in (getattr(node, "item_from", None), getattr(node, "item_to", None)):
                inner = getattr(wrapper, "data", None)
                if isinstance(inner, BaseFeedConfigModel):
                    stack.append(inner)

            items = getattr(node, "items", None)
            if isinstance(items, list):
                for item in items:
                    inner = item if isinstance(item, BaseFeedConfigModel) else getattr(item, "data", None)
                    if isinstance(inner, BaseFeedConfigModel):
                        stack.append(inner)

        return keys

    def _get_descendant_cursor_keys_cached(self) -> set[str]:
        cached = self._descendant_cursor_keys_cache
        if cached is None:
            cached = self._collect_descendant_cursor_keys(self.data)
            self._descendant_cursor_keys_cache = cached
        return cached

    def _reset_descendant_cursors(self, next_page: FeedResultNextPage) -> None:
        descendant_keys = self._get_descendant_cursor_keys_cached()
        CursorMap(next_page).reset_keys(descendant_keys)

    def _build_redis_state_key(self, user_id: Any, params: Dict[str, Any]) -> str:
        suffix = params.get("custom_deduplication_key") or params.get("custom_view_session_key")
        if suffix:
            return f"dedup:{self.merger_id}:{user_id}:{suffix}"
        return f"dedup:{self.merger_id}:{user_id}"

    def build_plan(
        self,
        *,
        ctx: ExecutionContext,
        limit: int,
        next_page: FeedResultNextPage,
        **params: Any,
    ) -> CallablePlan:
        async def _run(executor: Any) -> FeedResult:
            if limit <= 0:
                return FeedResult(data=[], next_page=next_page, has_next_page=False)

            if ctx.executor is None:
                ctx.executor = executor

            entry = next_page.data.get(self.merger_id)
            requested_page = entry.page if entry is not None else None
            is_fresh_session = requested_page is None or (isinstance(requested_page, int) and requested_page <= 0)

            redis_client = ctx.redis_client
            if self.state_backend == "redis" and not redis_client:
                raise ValueError("Redis client must be provided if using MergerDeduplication with state_backend=redis")

            working_next_page = _pydantic_deep_copy(next_page)
            if is_fresh_session:
                self._reset_descendant_cursors(working_next_page)

            seen_request_set: set[str] = set()
            store: SeenStore
            if self.state_backend == "cursor":
                cursor_entry = next_page.data.get(self.merger_id)
                store = CursorSeenStore.from_after(
                    cursor_entry.after if (cursor_entry is not None and not is_fresh_session) else None,
                    cursor_compress=self.cursor_compress,
                    cursor_max_keys=self.cursor_max_keys,
                )
            else:
                assert redis_client is not None
                redis_state_key = self._build_redis_state_key(user_id=ctx.user_id, params=params)
                store = RedisSeenStore.create(
                    redis_client=redis_client,
                    redis_key=redis_state_key,
                    ttl_seconds=self.state_ttl_seconds,
                )
                if is_fresh_session:
                    await store.reset()

            policy = DeduplicationPolicy(
                dedup_key=self.dedup_key,
                missing_key_policy=self.missing_key_policy,
                store=store,
                seen_request_set=seen_request_set,
            )

            refill_settings = RefillExecutionSettings(
                overfetch_factor=self.overfetch_factor,
                max_refill_loops=self.max_refill_loops,
            )
            child_ctx = ExecutionContext(
                methods_dict=ctx.methods_dict,
                user_id=ctx.user_id,
                redis_client=ctx.redis_client,
                executor=ctx.executor,
                dedup=policy,
                refill_settings=refill_settings,
            )

            child = _pydantic_deep_copy(self.data)
            child_result = await executor.run(child, child_ctx, limit, working_next_page, **params)

            commit_result: Any = await store.commit()
            merger_after: Any = commit_result if self.state_backend == "cursor" else None

            page = next_page.data[self.merger_id].page if self.merger_id in next_page.data else 1
            result_next_page = _pydantic_deep_copy(child_result.next_page)
            result_next_page.data[self.merger_id] = FeedResultNextPageInside(page=page + 1, after=merger_after)
            return FeedResult(
                data=child_result.data,
                next_page=result_next_page,
                has_next_page=child_result.has_next_page,
            )

        return CallablePlan(fn=_run)
