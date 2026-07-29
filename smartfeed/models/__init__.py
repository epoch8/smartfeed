from __future__ import annotations

from typing import Annotated, Dict, Union

from pydantic import BaseModel, Field, model_validator

from .base import BaseNode, FeedResult, MixerNode
from .mixers import (
    MergerAppend,
    MergerAppendDistribute,
    MergerPercentage,
    MergerPercentageGradient,
    MergerPercentageItem,
    MergerPositional,
)
from .subfeed import SubFeed
from .wrapper import Wrapper, WrapperCache, WrapperDedup, WrapperRerank

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


def _walk_ids(node: BaseNode, seen: Dict[str, str]) -> None:
    """Collect every subfeed_id/node_id in the tree, raising on a duplicate.

    Note the BaseNode check comes BEFORE the generic has-a-.data check: a Wrapper is
    both a node AND has .data, and must be visited itself, not skipped over.
    """
    node_id = getattr(node, "subfeed_id", None) or getattr(node, "node_id", None)
    if node_id is not None:
        kind = type(node).__name__
        if node_id in seen:
            raise ValueError(
                f"duplicate id '{node_id}' ({seen[node_id]} and {kind}): subfeed_id and node_id share "
                f"one namespace and must be unique across the config tree -- duplicates silently "
                f"collide in cursors and Redis keys"
            )
        seen[node_id] = kind
    for attr in ("items", "data", "positional", "default", "item_from", "item_to"):
        child = getattr(node, attr, None)
        if child is None or child is node:
            continue
        children = child if isinstance(child, list) else [child]
        for item in children:
            if isinstance(item, BaseNode):
                _walk_ids(item, seen)
            else:
                # MergerPercentageItem and friends wrap their node in .data
                inner = getattr(item, "data", None)
                if isinstance(inner, BaseNode):
                    _walk_ids(inner, seen)


class FeedConfig(BaseModel):
    # Top-level keys other than `feed` (e.g. a legacy `version` tag) are ignored.
    feed: FeedNode

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> "FeedConfig":
        seen: Dict[str, str] = {}
        _walk_ids(self.feed, seen)
        return self


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
    "MixerNode",
    "FeedResult",
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
