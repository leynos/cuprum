"""Shared test helpers for the tee profiling benchmark harness."""

from __future__ import annotations

import typing as typ

from hypothesis import strategies as st

from benchmarks import tee_profile_worker

if typ.TYPE_CHECKING:
    import threading

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
    """Recursively replace the values of nominated keys with '<redacted>'."""
    if isinstance(obj, dict):
        return {
            k: "<redacted>" if k in keys else redact(v, keys) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(item, keys) for item in obj]
    return obj


_EVENT_WAIT_TIMEOUT_SECONDS = 10
_THREAD_JOIN_TIMEOUT_SECONDS = 15


def _available_backend_names() -> tuple[tee_profile_worker.BackendName, ...]:
    """Return backend names that can run in this environment."""
    if tee_profile_worker._backend._check_rust_available():
        return ("auto", "python", "rust")
    return ("auto", "python")


def _alternate_backend() -> tee_profile_worker.BackendName:
    """Return the non-python backend available in this environment."""
    return "rust" if tee_profile_worker._backend._check_rust_available() else "auto"


@st.composite
def _backend_lists(
    draw: st.DrawFn,
) -> tuple[tee_profile_worker.BackendName, ...]:
    """Generate same-length backend sequences for concurrent worker tests."""
    thread_count = draw(st.integers(min_value=2, max_value=8))
    backend = st.sampled_from(_available_backend_names())
    return tuple(draw(st.lists(backend, min_size=thread_count, max_size=thread_count)))


_backends_strategy: st.SearchStrategy[tee_profile_worker.BackendName] = st.deferred(
    lambda: st.sampled_from(_available_backend_names())
)


def _join_and_assert_finished(
    *threads: threading.Thread,
    context: str = "",
) -> None:
    """Join *threads* and assert all have stopped within the configured timeout."""
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, (  # noqa: S101 - shared test helper asserts on caller's behalf.
        f"expected threads to finish{f' ({context})' if context else ''}, got {alive}"
    )
