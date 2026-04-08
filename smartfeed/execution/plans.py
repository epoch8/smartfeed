from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List


@dataclass
class MixChild:
    node_id: str
    node: Any  # BaseNode
    demand: int


@dataclass
class MixPlan:
    children: List[MixChild]
    assemble: Callable  # (buffers: dict[str,list], cursors: dict[str,dict]) -> (list, dict)
