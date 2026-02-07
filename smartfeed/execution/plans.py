from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

from ..feed_models import BaseFeedConfigModel, FeedResult, FeedResultNextPage
from .context import ExecutionContext


class Plan(Protocol):
    """Declarative execution plan.

    Plans describe what to run; the `Executor` is responsible for interpreting
    and executing them.
    """


@dataclass(frozen=True)
class CallablePlan:
    """A plan implemented as an async callable.

    Useful for mergers whose child limits depend on previous child results.
    """

    fn: Callable[["Executor"], Awaitable[FeedResult]]


@dataclass(frozen=True)
class SlotSpec:
    """A slot segment owned by a child node.

    Output order is defined by the sequence of SlotSpecs.
    """

    owner: BaseFeedConfigModel
    max_count: int


@dataclass(frozen=True)
class SlotsPlan:
    """Plan expressed as slot ownership + an assembly function.

    The executor will fetch children (possibly in priority order) and then assemble
    results in the slot schedule order.
    """

    ctx: ExecutionContext
    limit: int
    next_page: FeedResultNextPage
    params: Dict[str, Any]
    slots: List[SlotSpec]
    assemble: Callable[[List[Any], FeedResultNextPage, Dict[int, FeedResult]], Any]
    owner_fetch_limits: Optional[Dict[int, int]] = None


# NOTE: `Executor` is imported only for typing to avoid an import cycle.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .executor import Executor
