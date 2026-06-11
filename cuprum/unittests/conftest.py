"""Shared test helpers for the tee profiling benchmark harness."""

from __future__ import annotations

import threading

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

_EVENT_WAIT_TIMEOUT_SECONDS = 10
_THREAD_JOIN_TIMEOUT_SECONDS = 15


def redact(obj: object, keys: frozenset[str] = _VOLATILE_KEYS) -> object:
    """Recursively replace the values of nominated keys with '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item, keys) for item in obj]
    return obj


def join_and_assert_finished(
    *threads: threading.Thread,
    context: str = "",
) -> None:
    """Join *threads* and assert all have stopped within the configured timeout."""
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, (
        f"expected threads to finish{f' ({context})' if context else ''}, got {alive}"
    )
