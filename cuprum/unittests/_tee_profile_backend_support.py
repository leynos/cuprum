"""Backend-availability probing and Hypothesis strategies for tee-profile tests.

Split from the lock/selector concurrency harness in
``_tee_profile_concurrency_support`` so backend detection and the Hypothesis
parametrization helpers have a single, focused home shared by the concurrency
and reentrancy test modules.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import strategies as st

from cuprum import _backend

if typ.TYPE_CHECKING:
    from _pytest.mark.structures import ParameterSet

    from benchmarks import tee_profile_worker


def assert_worker_result_ok(
    result: tee_profile_worker.TeeProfileWorkerResult,
    *,
    expected_length: int | None = None,
    expected_lines: int | None = None,
) -> None:
    """Assert a worker payload reports a clean run with the expected output.

    Parameters
    ----------
    result : tee_profile_worker.TeeProfileWorkerResult
        The worker payload under test.
    expected_length : int or None, optional
        Required ``captured_output_length``; unchecked when ``None``.
    expected_lines : int or None, optional
        Required ``stdout_line_count``; unchecked when ``None``.
    """
    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
    if expected_length is not None:
        assert result["captured_output_length"] == expected_length, (
            f"expected captured output length {expected_length}, got {result}"
        )
    if expected_lines is not None:
        assert result["stdout_line_count"] == expected_lines, (
            f"expected {expected_lines} stdout line callbacks, got {result}"
        )


def _rust_backend_available() -> bool:
    """Report whether the Rust tee-profile backend can run in this environment."""
    return _backend._check_rust_available()


def _available_backend_names() -> tuple[tee_profile_worker.BackendName, ...]:
    """Return backend names that can run in this environment."""
    if _rust_backend_available():
        return ("auto", "python", "rust")
    return ("auto", "python")


def _alternate_backend() -> tee_profile_worker.BackendName:
    """Return the non-python backend available in this environment."""
    if _rust_backend_available():
        return "rust"
    msg = "Rust backend is unavailable; no non-python backend can be selected"
    raise RuntimeError(msg)


def _backend_pairs() -> tuple[
    tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName]
    | ParameterSet,
    ...,
]:
    """Return parameterized backend pairs for concurrent-worker tests."""
    try:
        alternate_backend = _alternate_backend()
    except RuntimeError as exc:
        alternate_pair = pytest.param(
            ("python", "rust"),
            marks=pytest.mark.skip(reason=str(exc)),
        )
    else:
        alternate_pair = ("python", alternate_backend)
    return (
        ("python", "python"),
        alternate_pair,
    )


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
