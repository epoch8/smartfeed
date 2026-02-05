"""Merger implementations.

Each merger schema lives in its own module.
`smartfeed.schemas` re-exports these classes for backwards compatibility.
"""

from .append import MergerAppend
from .append_distribute import MergerAppendDistribute
from .deduplication import MergerDeduplication
from .percentage import MergerPercentage, MergerPercentageItem
from .percentage_gradient import MergerPercentageGradient
from .positional import MergerPositional
from .view_session import MergerViewSession

__all__ = [
    "MergerAppend",
    "MergerAppendDistribute",
    "MergerDeduplication",
    "MergerPercentage",
    "MergerPercentageItem",
    "MergerPercentageGradient",
    "MergerPositional",
    "MergerViewSession",
]
