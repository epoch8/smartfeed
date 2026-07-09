from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Tuple

if TYPE_CHECKING:
    from smartfeed.models.base import BaseNode


@dataclass
class MixChild:
    node_id: str
    node: BaseNode
    demand: int


@dataclass
class MixPlan:
    children: List[MixChild]
    # (buffers: {child_id: items}, cursors: {child_id: cursor}) -> (merged items, merged cursor)
    assemble: Callable[[Dict[str, List[Any]], Dict[str, Dict[str, Any]]], Tuple[List[Any], Dict[str, Any]]]
