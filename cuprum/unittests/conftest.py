"""Shared test helpers for the tee profiling benchmark harness."""

from __future__ import annotations

_VOLATILE_KEYS: frozenset[str] = frozenset({
    "sha256",
    "wall_time_seconds",
    "output_bytes",
    "fixture_path",
    "wrapped_fixture_path",
    "output_dir",
    "profile_dir",
    "worker_command",
})


def redact(obj: object, keys: frozenset[str] = _VOLATILE_KEYS) -> object:
    """Recursively replace the values of nominated keys with '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item, keys) for item in obj]
    return obj
