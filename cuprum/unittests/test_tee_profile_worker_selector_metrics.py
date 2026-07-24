"""Tests for tee profile worker selector observability metrics.

The checks confirm selector contention counters are present and meaningful in
`run_tee_profile_worker` results and repeat-count scenarios.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests import _tee_profile_concurrency_support

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth


class _SequenceClock:
    """Deterministic clock that returns configured values in order."""

    def __init__(self, values: list[float]) -> None:
        """Store the deterministic timestamp sequence for later calls."""
        self._values = iter(values)

    def __call__(self) -> float:
        """Return the next configured timestamp."""
        return next(self._values)


def test_worker_result_includes_selector_metrics(tmp_path: pth.Path) -> None:
    """Worker results include selector observability metrics."""
    fixture = tmp_path / "fixture_metrics.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=1,
        ),
    )

    assert result["lock_wait_seconds"] >= 0.0, (
        f"expected non-negative lock wait metric, got {result}"
    )
    assert result["reentrant_rejection_count"] >= 0, (
        f"expected non-negative reentrant-rejection metric, got {result}"
    )


def test_worker_result_records_selector_lock_wait(tmp_path: pth.Path) -> None:
    """Worker result exposes the selector lock-wait duration."""
    fixture = tmp_path / "fixture_metrics_wait.b64"
    fixture.write_text("YWJjZGVm\n")
    selector = tee_profile_worker._EnvBackendSelector(
        clock=_SequenceClock([10.0, 10.25]),
    )

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=1,
        ),
        backend_selector=selector,
    )

    assert result["lock_wait_seconds"] == pytest.approx(0.25), (
        f"expected deterministic selector lock wait, got {result}"
    )


def test_worker_clock_drives_wall_time_and_selector_lock_wait(
    tmp_path: pth.Path,
) -> None:
    """Injected worker clock also drives default selector lock-wait timing."""
    fixture = tmp_path / "fixture_worker_clock.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=1,
        ),
        clock=_SequenceClock([100.0, 101.0, 101.5, 104.0]),
    )

    assert result["wall_time_seconds"] == pytest.approx(4.0), (
        f"expected deterministic worker wall time, got {result}"
    )
    assert result["lock_wait_seconds"] == pytest.approx(0.5), (
        f"expected worker clock to drive selector lock wait, got {result}"
    )


def test_worker_accumulates_repeat_counters(tmp_path: pth.Path) -> None:
    """Worker output counters accumulate over repeated measured runs."""
    fixture = tmp_path / "fixture_repeat.b64"
    fixture.write_text("YWJjZGVm\n")

    result = run_tee_profile_worker(
        TeeProfileWorkerConfig(
            fixture_path=fixture,
            stages=1,
            mode="tee",
            sink_kind="devnull",
            with_line_callbacks=True,
            backend="python",
            repeat_count=3,
        ),
    )

    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
    assert result["captured_output_length"] == len(fixture.read_text()) * 3, (
        "expected captured output length to accumulate across repeats, "
        f"got {result['captured_output_length']}"
    )
    assert result["stdout_line_count"] == 3, (
        f"expected line callbacks to accumulate across repeats, got {result}"
    )


def test_worker_resets_prepopulated_selector_metrics(
    tmp_path: pth.Path,
) -> None:
    """run_tee_profile_worker resets stale metrics carried by the selector."""

    class _PassthroughBackendSelector(
        _tee_profile_concurrency_support._BaseBackendSelector,
    ):
        """Delegate directly while exposing the helper selector metrics state."""

        def __init__(self) -> None:
            """Initialise the helper base with deterministic selector timing."""
            super().__init__({}, [], threading.Lock())
            self._delegate = tee_profile_worker._EnvBackendSelector(
                clock=_SequenceClock([0.0, 0.0]),
            )

        @contextlib.contextmanager
        def _activate(
            self,
            backend: tee_profile_worker.BackendName,
        ) -> cabc.Iterator[None]:
            """Enter the real selector without adding coordination events."""
            with self._delegate(backend):
                yield

    fixture = tmp_path / "fixture_reset_metrics.b64"
    fixture.write_text("YWJjZGVm\n")
    selector = _PassthroughBackendSelector()

    selector.metrics_state.add_lock_wait(99.0)
    selector.metrics_state.increment_rejections()
    selector.metrics_state.increment_rejections()

    config = tee_profile_worker.TeeProfileWorkerConfig(
        fixture_path=fixture,
        stages=1,
        mode="echo",
        sink_kind="devnull",
        with_line_callbacks=False,
        backend="python",
        repeat_count=1,
    )
    result = tee_profile_worker.run_tee_profile_worker(
        config,
        backend_selector=selector,
    )

    assert result["lock_wait_seconds"] == pytest.approx(0.0, abs=1e-6)
    assert result["reentrant_rejection_count"] == 0


def test_metrics_are_thread_local() -> None:
    """Selector metrics remain isolated between threads."""
    metrics_state = tee_profile_worker._MetricsState(threading.local())
    snapshots: dict[str, tee_profile_worker._SelectorMetrics] = {}

    def record_active_thread_metrics() -> None:
        """Populate metrics on a non-main thread for isolation checks."""
        metrics_state.reset()
        metrics_state.add_lock_wait(0.5)
        metrics_state.increment_rejections()
        snapshots["active"] = metrics_state.snapshot()

    thread = threading.Thread(target=record_active_thread_metrics)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive(), "expected metrics thread to finish"
    snapshots["main"] = metrics_state.snapshot()

    assert snapshots["active"].lock_wait_seconds == pytest.approx(0.5)
    assert snapshots["active"].reentrant_rejection_count == 1
    assert snapshots["main"].lock_wait_seconds == pytest.approx(0.0)
    assert snapshots["main"].reentrant_rejection_count == 0


@given(
    waits=st.lists(
        st.floats(
            min_value=0.0,
            max_value=10.0,
            allow_nan=False,
            allow_infinity=False,
            width=32,
        ),
        max_size=20,
    ),
    rejections=st.integers(min_value=0, max_value=20),
)
def test_metrics_accumulate_and_reset(
    waits: list[float],
    rejections: int,
) -> None:
    """Selector metrics preserve accumulation and reset invariants."""
    metrics_state = tee_profile_worker._MetricsState(threading.local())
    metrics_state.reset()
    previous_wait = 0.0

    for wait in waits:
        metrics_state.add_lock_wait(wait)
        snapshot = metrics_state.snapshot()
        assert snapshot.lock_wait_seconds >= previous_wait
        assert snapshot.reentrant_rejection_count == 0
        previous_wait = snapshot.lock_wait_seconds

    for expected_rejections in range(1, rejections + 1):
        assert metrics_state.increment_rejections() == expected_rejections

    snapshot = metrics_state.snapshot()
    assert snapshot.lock_wait_seconds == pytest.approx(sum(waits))
    assert snapshot.reentrant_rejection_count == rejections

    metrics_state.reset()
    reset_snapshot = metrics_state.snapshot()
    assert reset_snapshot.lock_wait_seconds == pytest.approx(0.0)
    assert reset_snapshot.reentrant_rejection_count == 0


def test_reentrant_rejection_increments_counter() -> None:
    """Nested backend selector entry increments the rejection metric."""
    metrics_state = tee_profile_worker._MetricsState(threading.local())
    selector = tee_profile_worker._EnvBackendSelector(metrics_state=metrics_state)
    metrics_state.reset()

    with (
        selector("python"),
        contextlib.suppress(RuntimeError),
        selector("auto"),
    ):
        pass

    metrics = metrics_state.snapshot()
    assert metrics.reentrant_rejection_count >= 1, (
        f"expected at least one reentrant rejection, got {metrics}"
    )


def test_repeated_reentrant_rejections_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated nested selector failures escalate to an error log."""
    metrics_state = tee_profile_worker._MetricsState(threading.local())
    selector = tee_profile_worker._EnvBackendSelector(metrics_state=metrics_state)
    metrics_state.reset()

    with (
        caplog.at_level(logging.ERROR, logger="benchmarks.tee_profile_worker"),
        selector("python"),
    ):
        for _ in range(2):
            with contextlib.suppress(RuntimeError), selector("auto"):
                pass

    error_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "Repeated re-entrant backend selector rejection" in record.getMessage()
    ]
    assert error_records, "expected repeated reentrant rejection to log an error"
