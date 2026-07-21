"""Shared JSON validation helpers for benchmark utilities."""

from __future__ import annotations

import math
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _require_mapping(value: object, *, name: str) -> cabc.Mapping[str, object]:
    """Validate and return a mapping-like JSON object."""
    if not isinstance(value, dict):
        msg = f"{name} must be an object"
        raise TypeError(msg)
    return typ.cast("dict[str, object]", value)


def _require_list(value: object, *, name: str) -> list[object]:
    """Validate and return a list value."""
    if not isinstance(value, list):
        msg = f"{name} must be a list"
        raise TypeError(msg)
    return typ.cast("list[object]", value)


def _require_non_empty_string(value: object, *, name: str) -> str:
    """Validate and return a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _require_bool(value: object, *, name: str) -> bool:
    """Validate and return a boolean value."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool"
        raise TypeError(msg)
    return value


def _check_float_bound(
    validated: float,
    *,
    name: str,
    minimum: float,
    exclusive: bool,
) -> None:
    """Raise ``ValueError`` when *validated* does not satisfy the minimum bound."""
    minimum_text = f"{minimum:g}"
    if exclusive:
        if validated <= minimum:
            msg = f"{name} must be > {minimum_text}"
            raise ValueError(msg)
    elif validated < minimum:
        msg = f"{name} must be >= {minimum_text}"
        raise ValueError(msg)


def _require_float(
    value: object,
    *,
    name: str,
    minimum: float,
    exclusive: bool,
) -> float:
    """Validate that *value* is a finite float satisfying a minimum bound."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        msg = f"{name} must be a number"
        raise TypeError(msg)
    validated = float(value)
    if not math.isfinite(validated):
        msg = f"{name} must be finite"
        raise ValueError(msg)
    _check_float_bound(validated, name=name, minimum=minimum, exclusive=exclusive)
    return validated


def _require_positive_float(value: object, *, name: str) -> float:
    """Validate and return a positive float value."""
    return _require_float(value, name=name, minimum=0.0, exclusive=True)


def _require_non_negative_float(value: object, *, name: str) -> float:
    """Validate and return a non-negative float value."""
    return _require_float(value, name=name, minimum=0.0, exclusive=False)
