"""Shared pytest configuration and helpers for Cuprum unit tests.

The module imports hypothesis_crosshair_provider so Hypothesis can use the
CrossHair backend, registers the local ``crosshair`` Hypothesis profile, and
declares the matching pytest marker for symbolic helper-property checks.
"""

from __future__ import annotations

import typing as typ

import hypothesis_crosshair_provider  # noqa: F401  # Registers CrossHair backend provider on import.
from hypothesis import settings

if typ.TYPE_CHECKING:
    import pytest

settings.register_profile(
    "crosshair",
    settings(backend="crosshair", deadline=None, derandomize=True, max_examples=50),
)

_VOLATILE_KEYS: frozenset[str] = frozenset({
    "sha256",
    "wall_time_seconds",
    "lock_wait_seconds",
    "output_bytes",
    "fixture_path",
    "wrapped_fixture_path",
    "output_dir",
    "profile_dir",
    "worker_command",
})


def redact(obj: object, keys: frozenset[str] = _VOLATILE_KEYS) -> object:
    """Recursively replace the values of nominated keys with '<redacted>'.

    Parameters
    ----------
    obj : object
        The recursively traversed value; only dictionaries and lists are
        copied recursively, while non-container values are returned
        unchanged.
    keys : frozenset[str]
        Dictionary-key names whose associated values are replaced with
        ``"<redacted>"``; defaults to ``_VOLATILE_KEYS``.

    Returns
    -------
    object
        A recursively copied dictionary or list with nominated keys redacted,
        or the original non-container value unchanged.
    """
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item, keys) for item in obj]
    return obj


def pytest_configure(config: pytest.Config) -> None:
    """Register local pytest markers."""
    config.addinivalue_line(
        "markers",
        "crosshair: property tests suitable for Hypothesis' CrossHair backend",
    )
