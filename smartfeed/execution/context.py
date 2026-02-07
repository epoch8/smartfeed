from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Union

import redis
from redis.asyncio import Redis as AsyncRedis


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
    dedup: Optional[object] = None

    # Execution settings (optional)
    refill_settings: Optional["RefillExecutionSettings"] = None
    dedup_settings: Optional["DedupExecutionSettings"] = None


@dataclass(frozen=True)
class RefillExecutionSettings:
    overfetch_factor: int = 1
    max_refill_loops: int = 20


DedupExecutionSettings = RefillExecutionSettings
