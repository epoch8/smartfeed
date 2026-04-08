from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SmartFeedDebugInfo(BaseModel):
    """Metadata stamped by SmartFeed on every item."""

    # Always present (stamped by SubFeed)
    source: str                                  # subfeed_id (regular_tours, recommended_tours, promo_tours)

    # Stamped by FeedManager (top-level position in final feed)
    smartfeed_position: Optional[int] = None     # 0-based position in page

    # Stamped by subfeed methods (optional, source-specific)
    strategy: Optional[str] = None               # ML model strategy (model_hot_users, model_cold_users)

    # Stamped by rerank callable (optional, present only when rerank is configured)
    rerank_position: Optional[int] = None        # position after rerank (1-based)
    rrf_score: Optional[float] = None            # RRF score
    feature_score: Optional[float] = None        # feature score from ES coefficients
    feature_position: Optional[int] = None       # rank by feature score (1-based)
    total_reranked: Optional[int] = None         # total items in rerank batch
    raw_params: Optional[Dict[str, Any]] = None  # raw tour params from Redis

    model_config = ConfigDict(extra="allow")


class FeedItem(BaseModel):
    """One item in the feed output. Wraps tour data + SmartFeed metadata."""

    id: Any
    smartfeed_debug_info: Optional[SmartFeedDebugInfo] = Field(
        default=None, alias="_smartfeed_debug_info"
    )

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class BaseNode(BaseModel):
    dedup_priority: int = 0

    def config_hash(self) -> str:
        # Use json.dumps with sort_keys for deterministic hashing
        raw = json.dumps(self.model_dump(), sort_keys=True, default=str)
        return hashlib.md5(raw.encode()).hexdigest()[:8]


class FeedResult(BaseModel):
    data: List
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
    from smartfeed.models import FeedNode  # noqa: PLC0415
    from pydantic import TypeAdapter
    return TypeAdapter(FeedNode).validate_python(value)
