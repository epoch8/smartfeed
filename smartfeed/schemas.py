"""Public schema surface.

This module keeps the public import path (`smartfeed.schemas`) stable while
moving merger implementations into `smartfeed.mergers.*`.
"""

from __future__ import annotations

from typing import Annotated, Any, Union

from pydantic import BaseModel, Field

from .feed_models import (
    BaseFeedConfigModel,
    FeedResult,
    FeedResultClient,
    FeedResultNextPage,
    FeedResultNextPageInside,
    SubFeed,
)
from .mergers import (
    MergerAppend,
    MergerAppendDistribute,
    MergerDeduplication,
    MergerPercentage,
    MergerPercentageGradient,
    MergerPercentageItem,
    MergerPositional,
    MergerViewSession,
)

FeedTypes = Annotated[
    Union[
        MergerDeduplication,
        MergerAppend,
        MergerAppendDistribute,
        MergerPositional,
        MergerPercentage,
        MergerPercentageGradient,
        MergerViewSession,
        SubFeed,
    ],
    Field(discriminator="type"),
]


class FeedConfig(BaseModel):
    """Top-level feed config model."""

    version: str
    feed: FeedTypes


def _rebuild_model(model: Any) -> None:
    """Resolve forward refs across modules (Pydantic v1/v2 compatible)."""

    if hasattr(model, "model_rebuild"):
        model.model_rebuild(force=True, _types_namespace={"FeedTypes": FeedTypes})
    else:
        model.update_forward_refs(FeedTypes=FeedTypes)


for _m in (
    MergerPositional,
    MergerPercentage,
    MergerPercentageItem,
    MergerAppend,
    MergerAppendDistribute,
    MergerPercentageGradient,
    MergerViewSession,
    MergerDeduplication,
    SubFeed,
    FeedConfig,
):
    _rebuild_model(_m)


__all__ = [
    "BaseFeedConfigModel",
    "FeedResult",
    "FeedResultClient",
    "FeedResultNextPage",
    "FeedResultNextPageInside",
    "SubFeed",
    "MergerAppend",
    "MergerAppendDistribute",
    "MergerDeduplication",
    "MergerPercentage",
    "MergerPercentageGradient",
    "MergerPercentageItem",
    "MergerPositional",
    "MergerViewSession",
    "FeedTypes",
    "FeedConfig",
]
