from __future__ import annotations

from random import shuffle
from typing import Any, Dict, List, Literal

from .base import BaseNode, FeedResult


class SubFeed(BaseNode):
    type: Literal["subfeed"] = "subfeed"
    subfeed_id: str
    method_name: str
    subfeed_params: Dict[str, Any] = {}
    raise_error: bool = True
    shuffle: bool = False

    async def execute(
        self,
        methods_dict: dict,
        session_id: str,
        limit: int,
        cursor: dict,
        **params: Any,
    ) -> FeedResult:
        method = methods_dict[self.method_name]
        subfeed_cursor = cursor.get(self.subfeed_id, {})

        try:
            result: FeedResult = await method(
                user_id=session_id,
                limit=limit,
                next_page=subfeed_cursor,
                **params,
                **self.subfeed_params,
            )
        except Exception:
            if self.raise_error:
                raise
            return FeedResult(data=[], next_page=cursor, has_next_page=False)

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
