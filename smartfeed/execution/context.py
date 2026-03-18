from __future__ import annotations

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis

if TYPE_CHECKING:
    from ..policies.dedup import DeduplicationPolicy


@dataclass
class ExecutionContext:
    """Execution context propagated through the feed tree.

    Keeps internal state (policies, backends) out of user params.
    """

    methods_dict: Dict[str, Callable]
    user_id: Any
    redis_client: Optional[Union[redis.Redis, AsyncRedis]] = None

    # Assigned by the caller (FeedManager / tests) to avoid circular imports.
    executor: Any = None

    # Policies (optional)
    dedup: Optional[DeduplicationPolicy] = None

    # Execution settings (optional)
    refill_settings: Optional["RefillExecutionSettings"] = None

    def ensure_redis_client(self, redis_client: Optional[Union[redis.Redis, AsyncRedis]]) -> None:
        if self.redis_client is None and redis_client is not None:
            self.redis_client = redis_client

    def ensure_executor(self) -> Any:
        if self.executor is None:
            from .executor import Executor

            self.executor = Executor()
        return self.executor


@dataclass(frozen=True)
class RefillExecutionSettings:
    overfetch_factor: int = 1
    max_refill_loops: int = 20
