from __future__ import annotations

from typing import Any, Dict, Optional

from .execution.context import ExecutionContext
from .execution import executor as _executor
from .models import FeedConfig
from .models.base import FeedResult


class FeedManager:
    def __init__(
        self,
        config: Dict,
        methods_dict: Dict,
        redis_client: Optional[Any] = None,
    ) -> None:
        if hasattr(FeedConfig, "model_validate"):
            # Pydantic v2
            self.config = FeedConfig.model_validate(config)
        else:
            # Pydantic v1
            self.config = FeedConfig.parse_obj(config)
        self.methods_dict = methods_dict
        self.redis_client = redis_client

    async def get_feed(
        self,
        session_id: str,
        limit: int,
        cursor: Optional[Dict] = None,
    ) -> FeedResult:
        cursor = cursor or {}
        ctx = ExecutionContext(
            session_id=session_id,
            methods_dict=self.methods_dict,
            redis=self.redis_client,
        )
        result = await _executor.run(self.config.feed, ctx, limit, cursor)
        for i, item in enumerate(result.data):
            if isinstance(item, dict):
                item.setdefault("_smartfeed_debug_info", {})["smartfeed_position"] = i
        return result
