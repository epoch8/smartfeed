from __future__ import annotations

from typing import Any, Callable, Optional

import orjson

DefaultFn = Callable[[Any], Any]


def dumps(
    obj: Any,
    *,
    default: Optional[DefaultFn] = None,
    sort_keys: bool = False,
) -> str:
    """Serialize *obj* to JSON text using orjson.

    This is a small compatibility layer meant to cover the subset of the stdlib
    `json.dumps` API used inside this package.

    Key differences vs `orjson.dumps`:
    - Returns `str` (UTF-8) instead of `bytes`.
    - Supports `default=` and `sort_keys=`.
    """

    option = 0
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS

    return orjson.dumps(obj, default=default, option=option).decode("utf-8")


def loads(data: Any) -> Any:
    """Deserialize JSON from *data* using orjson.

    Accepts `str`, `bytes`, `bytearray`, or `memoryview` (same spirit as
    stdlib `json.loads`).
    """

    return orjson.loads(data)
