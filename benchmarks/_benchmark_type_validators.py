"""Shared validation helpers for benchmark configuration modules.

This module centralises small type-, enum-, path-, and bounds-validation
helpers used by benchmark configuration dataclasses. These helpers were
extracted from ``benchmarks._benchmark_types``,
``benchmarks.tee_profile_scenarios``, and related benchmark modules so
validation limits, error semantics, and fallibility contracts stay consistent
across pipeline-throughput and tee-profile configuration surfaces.

Each helper validates a single value and returns it unchanged (often narrowed
to a concrete type) when the input is acceptable. On invalid input a helper
raises either ``TypeError`` or ``ValueError`` according to its own contract:
some reject wrong types with ``TypeError`` (for example ``_validate_int``),
while others raise ``ValueError`` for an unsupported type/value combination or
an out-of-range value (for example ``_validate_backend`` rejects a non-string
backend, and ``_validate_payload_bytes`` rejects a non-positive size, both with
``ValueError``). The dataclasses call these helpers from their ``__post_init__``
methods so invalid configuration fails fast at construction time.

Examples
--------
>>> from benchmarks._benchmark_type_validators import _validate_payload_bytes
>>> _validate_payload_bytes(1024)
1024
>>> try:
...     _validate_payload_bytes(0)
... except ValueError as exc:
...     print(exc)
payload_bytes must be > 0, got 0
"""

from __future__ import annotations

import pathlib as pth
import typing as typ

_MAX_HYPERFINE_ITERATIONS: typ.Final = 1000
_MIN_PIPELINE_STAGES: typ.Final = 2
_VALID_BACKENDS: typ.Final = {"python", "rust"}


def _validate_int(value: object, *, name: str) -> int:
    """Validate integer values and reject booleans."""
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"{name} must be an int, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _validate_bool(value: object, *, name: str) -> bool:
    """Validate boolean values."""
    if not isinstance(value, bool):
        msg = f"{name} must be a bool, got {type(value).__name__}"
        raise TypeError(msg)
    return value


def _validate_non_empty_string(value: object, *, name: str) -> str:
    """Validate non-empty string values."""
    if not isinstance(value, str) or not value.strip():
        msg = f"{name} must be a non-empty string"
        raise ValueError(msg)
    return value


def _validate_path(value: object, *, name: str) -> pth.Path:
    """Validate path-like values and normalise them as ``pathlib.Path``."""
    if isinstance(value, pth.Path):
        return value
    try:
        return pth.Path(typ.cast("typ.Any", value))
    except TypeError as exc:
        msg = (
            f"{name} must be a pathlib.Path or path-like value, "
            f"got {type(value).__name__}"
        )
        raise TypeError(msg) from exc


def _validate_iteration_count(
    value: object,
    *,
    name: str,
    min_value: int,
) -> int:
    """Validate a single hyperfine iteration count against its bounds."""
    validated = _validate_int(value, name=name)
    if validated < min_value:
        msg = f"{name} must be >= {min_value}, got {validated}"
        raise ValueError(msg)
    if validated > _MAX_HYPERFINE_ITERATIONS:
        msg = f"{name} must be <= {_MAX_HYPERFINE_ITERATIONS}, got {validated}"
        raise ValueError(msg)
    return validated


def _validate_minimum_int(value: object, *, name: str, min_value: int) -> int:
    """Validate one integer count against a lower bound."""
    validated = _validate_int(value, name=name)
    if validated < min_value:
        msg = f"{name} must be >= {min_value}, got {validated}"
        raise ValueError(msg)
    return validated


def _validate_hyperfine_iterations(*, warmup: object, runs: object) -> None:
    """Validate hyperfine warmup and run counts."""
    _validate_iteration_count(warmup, name="warmup", min_value=0)
    _validate_iteration_count(runs, name="runs", min_value=1)


def _validate_scenario_name(value: object) -> str:
    """Validate that a scenario name is a non-empty string."""
    if not isinstance(value, str) or not value.strip():
        msg = "name must be a non-empty string"
        raise ValueError(msg)
    return value


def _validate_payload_bytes(value: object) -> int:
    """Validate that scenario payload size is a positive integer."""
    payload_bytes = _validate_int(value, name="payload_bytes")
    if payload_bytes <= 0:
        msg = f"payload_bytes must be > 0, got {payload_bytes}"
        raise ValueError(msg)
    return payload_bytes


def _validate_stages(value: object) -> int:
    """Validate that scenario stage count meets the minimum threshold."""
    stages = _validate_int(value, name="stages")
    if stages < _MIN_PIPELINE_STAGES:
        msg = f"stages must be >= {_MIN_PIPELINE_STAGES}, got {stages}"
        raise ValueError(msg)
    return stages


def _validate_backend(value: object) -> str:
    """Validate that a scenario backend is one of the supported values."""
    # Check ``isinstance`` first and rely on short-circuit evaluation so the
    # membership test never receives an unhashable object (for example ``[]``
    # or ``{}``), which would raise ``TypeError`` instead of the ``ValueError``
    # callers expect.
    if not isinstance(value, str) or value not in _VALID_BACKENDS:
        msg = f"backend must be one of {sorted(_VALID_BACKENDS)}, got {value!r}"
        raise ValueError(msg)
    return value


__all__ = [
    "_MAX_HYPERFINE_ITERATIONS",
    "_MIN_PIPELINE_STAGES",
    "_VALID_BACKENDS",
    "_validate_backend",
    "_validate_bool",
    "_validate_hyperfine_iterations",
    "_validate_int",
    "_validate_iteration_count",
    "_validate_minimum_int",
    "_validate_non_empty_string",
    "_validate_path",
    "_validate_payload_bytes",
    "_validate_scenario_name",
    "_validate_stages",
]
