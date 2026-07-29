from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from pydantic import BaseModel, PrivateAttr

if TYPE_CHECKING:
    from smartfeed.execution.context import ExecutionContext
    from smartfeed.execution.plans import MixPlan


class BaseNode(BaseModel):
    dedup_priority: int = 0

    # Config is immutable once FeedManager validates it, so the hash is computed once
    # per node instance instead of on every Redis key construction.
    _config_hash: Optional[str] = PrivateAttr(default=None)

    def config_hash(self) -> str:
        if self._config_hash is None:
            # json.dumps with sort_keys for deterministic hashing
            raw = json.dumps(self.model_dump(), sort_keys=True, default=str)
            self._config_hash = hashlib.md5(raw.encode()).hexdigest()[:8]
        return self._config_hash

    async def execute(
        self,
        methods_dict: Dict[str, Any],
        session_id: str,
        limit: int,
        cursor: Dict[str, Any],
        ctx: Optional[ExecutionContext] = None,
        **params: Any,
    ) -> FeedResult:
        """Leaf/pipeline nodes (SubFeed, Wrapper) override this; mixers build a MixPlan instead."""
        raise NotImplementedError(f"{type(self).__name__} does not execute directly")


class MixerNode(BaseNode):
    """Base for combiner nodes. The executor dispatches on this type: mixers return a
    MixPlan of children to run concurrently instead of executing directly."""

    def build_mix_plan(self, *, ctx: ExecutionContext, limit: int, cursor: Dict[str, Any]) -> MixPlan:
        raise NotImplementedError


class FeedResult(BaseModel):
    # Items are usually dicts; non-dict items are passed through untouched by
    # stamping/dedup, so the element type is deliberately Any.
    data: List[Any]
    next_page: Dict[str, Any]
    has_next_page: bool


def coerce_feed_node(value: Any) -> Any:
    """Coerce a dict to the appropriate FeedNode model instance.

    Uses a lazy import of FeedNode to avoid circular imports.
    Non-dict values are returned unchanged.
    """
    if not isinstance(value, dict):
        return value
    # Lazy import to break circular dependency
    from pydantic import TypeAdapter

    from smartfeed.models import FeedNode

    return TypeAdapter(FeedNode).validate_python(value)
