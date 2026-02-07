from __future__ import annotations

from typing import Any, Mapping, Type, TypeVar

T = TypeVar("T")


def parse_model(model_cls: Type[T], obj: Mapping[str, Any]) -> T:
    """Parse a mapping into a Pydantic model.

    Uses Pydantic v2 `model_validate` when available, otherwise falls back to v1 `parse_obj`.
    """

    if hasattr(model_cls, "model_validate"):
        return model_cls.model_validate(obj)  # type: ignore[attr-defined]
    return model_cls.parse_obj(obj)  # type: ignore[attr-defined]
