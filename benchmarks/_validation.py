"""Shared JSON validation helpers for benchmark utilities."""

from __future__ import annotations

import typing as typ


def _require_mapping(value: object, *, name: str) -> typ.Mapping[str, object]:
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
