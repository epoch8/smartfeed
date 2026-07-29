from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from redis.asyncio import Redis as AsyncRedis


@dataclass
class ExecutionContext:
    session_id: str
    methods_dict: Dict[str, Callable[..., Any]]
    redis: Optional[AsyncRedis[Any]] = None
