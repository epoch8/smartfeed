from __future__ import annotations

from random import shuffle
from typing import TYPE_CHECKING, Any, Dict, Literal, Optional

from .base import BaseNode, FeedResult

if TYPE_CHECKING:
    from smartfeed.execution.context import ExecutionContext


class SubFeed(BaseNode):
    type: Literal["subfeed"] = "subfeed"
    subfeed_id: str
    method_name: str
    subfeed_params: Dict[str, Any] = {}
    raise_error: bool = True
    shuffle: bool = False

    async def execute(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        cursor: Dict[str, Any],
        ctx: Optional[ExecutionContext] = None,
        **params: Any,
    ) -> FeedResult:
        method = methods_dict[self.method_name]
        subfeed_cursor = cursor.get(self.subfeed_id, {})
        # Like the missing-method KeyError, this raises regardless of raise_error:
        # a malformed cursor is client tampering, not a source failure to fail soft on.
        if not isinstance(subfeed_cursor, dict):
            raise ValueError(
                f"SubFeed '{self.subfeed_id}': cursor entry must be a dict, got {type(subfeed_cursor).__name__}"
            )

        try:
            result: FeedResult = await method(
                user_id=session_id,
                limit=limit,
                next_page=subfeed_cursor,
                ctx=ctx,
                **params,
                **self.subfeed_params,
            )
        except Exception:
            if self.raise_error:
                raise
            # Scope the failure cursor to THIS subfeed (its own, unadvanced position)
            # so it retries next page without clobbering sibling cursors on merge.
            return FeedResult(data=[], next_page={self.subfeed_id: subfeed_cursor}, has_next_page=False)

        if self.shuffle:
            shuffle(result.data)

        # Stamp source on every dict item (merge, don't overwrite)
        for item in result.data:
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {})["source"] = self.subfeed_id

        return FeedResult(
            data=result.data,
            next_page={self.subfeed_id: result.next_page},
            has_next_page=result.has_next_page,
        )
