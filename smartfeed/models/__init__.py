from __future__ import annotations

from typing import Annotated, Union

from pydantic import Field

from .base import BaseNode, FeedResult, SmartFeedDebugInfo, FeedItem
from .subfeed import SubFeed
from .mixers import (
    MergerPercentage,
    MergerPercentageItem,
    MergerAppend,
    MergerPositional,
    MergerPercentageGradient,
    MergerAppendDistribute,
)
from .wrapper import Wrapper, WrapperCache, WrapperRerank, WrapperDedup

# ---------------------------------------------------------------------------
# FeedNode: discriminated union of all node types
# ---------------------------------------------------------------------------

FeedNode = Annotated[
    Union[
        SubFeed,
        Wrapper,
        MergerAppend,
        MergerPercentage,
        MergerPercentageGradient,
        MergerPositional,
        MergerAppendDistribute,
    ],
    Field(discriminator="type"),
]

# ---------------------------------------------------------------------------
# FeedConfig: top-level config model
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class FeedConfig(BaseModel):
    version: str
    feed: FeedNode


# Rebuild forward refs so that nested FeedNode fields resolve correctly
_types_ns = {"FeedNode": FeedNode}
for _model in (
    SubFeed,
    Wrapper,
    WrapperCache,
    MergerAppend,
    MergerPercentage,
    MergerPercentageItem,
    MergerPositional,
    MergerPercentageGradient,
    MergerAppendDistribute,
    FeedConfig,
):
    _model.model_rebuild(force=True, _types_namespace=_types_ns)

__all__ = [
    "BaseNode",
    "FeedResult",
    "SmartFeedDebugInfo",
    "FeedItem",
    "SubFeed",
    "MergerPercentage",
    "MergerPercentageItem",
    "MergerAppend",
    "MergerPositional",
    "MergerPercentageGradient",
    "MergerAppendDistribute",
    "Wrapper",
    "WrapperCache",
    "WrapperRerank",
    "WrapperDedup",
    "FeedNode",
    "FeedConfig",
]
