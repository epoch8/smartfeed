from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from redis.asyncio import Redis as AsyncRedis

from .execution import executor as _executor
from .execution.context import ExecutionContext
from .models import FeedConfig
from .models.base import FeedResult


class FeedManager:
    def __init__(
        self,
        config: Dict[str, Any],
        methods_dict: Dict[str, Callable[..., Any]],
        redis_client: Optional[AsyncRedis[Any]] = None,
    ) -> None:
        self.config = FeedConfig.model_validate(config)
        self.methods_dict = methods_dict
        self.redis_client = redis_client

    async def get_feed(
        self,
        session_id: str,
        limit: int,
        cursor: Optional[Dict[str, Any]] = None,
    ) -> FeedResult:
        # Boundary guards: these inputs come from the integrator's request handler
        # (and the cursor round-trips through untrusted clients); fail loudly here
        # instead of slicing/keying garbage deeper in the pipeline.
        if not isinstance(session_id, str) or not session_id:
            raise ValueError(f"session_id must be a non-empty string, got {session_id!r}")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError(f"limit must be a positive int, got {limit!r}")
        cursor = self._validated_cursor(cursor)
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

    @staticmethod
    def _validated_cursor(cursor: object) -> Dict[str, Any]:
        """Runtime gate for the declared Optional[dict] contract.

        Untyped callers can pass anything, so the check runs on an `object` view:
        checking the already-narrowed parameter would make the raise provably dead
        code to type analysis (pyright reportUnreachable).
        """
        if cursor is None:
            return {}
        if not isinstance(cursor, dict):
            raise TypeError(
                f"cursor must be the dict from the previous FeedResult.next_page (or None), "
                f"got {type(cursor).__name__}"
            )
        return cursor
