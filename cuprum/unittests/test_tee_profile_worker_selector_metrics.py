"""Tests for tee profile worker selector observability metrics.

The checks confirm selector contention counters are present and meaningful in
`run_tee_profile_worker` results and repeat-count scenarios.
"""

from __future__ import annotations

import contextlib
import logging
import queue
import threading
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks import _tee_profile_worker_backend, tee_profile_worker
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
    selector = _tee_profile_worker_backend._EnvBackendSelector(
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


def test_worker_resets_prepopulated_selector_metrics(
    tmp_path: pth.Path,
) -> None:
    """run_tee_profile_worker resets stale metrics carried by the selector."""

    class _PassthroughBackendSelector(
        _tee_profile_concurrency_support._BaseBackendSelector,
    ):
        """Delegate directly while exposing the helper selector metrics state."""

        def __init__(self) -> None:
            """Initialize the helper base with deterministic selector timing."""
            super().__init__({}, [], threading.Lock())
            self._delegate = _tee_profile_worker_backend._EnvBackendSelector(
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
    metrics_state = _tee_profile_worker_backend._MetricsState(threading.local())
    # The queue is the ownership boundary: the worker hands its snapshot over
    # rather than mutating a container the main thread also writes to.
    snapshot_queue: queue.Queue[_tee_profile_worker_backend._SelectorMetrics] = (
        queue.Queue()
    )
    worker_ready = threading.Event()
    main_snapshot_taken = threading.Event()

    def record_active_thread_metrics() -> None:
        """Publish this thread's metrics, then wait for the main thread."""
        metrics_state.reset()
        metrics_state.add_lock_wait(0.5)
        metrics_state.increment_rejections()
        snapshot_queue.put(metrics_state.snapshot())
        worker_ready.set()
        # Stay alive until the main thread has sampled its own metrics, so the
        # isolation holds while both threads are live rather than only after
        # this one has exited. The wait is unbounded on purpose: the main
        # thread's finally block always sets the event, so a timeout here would
        # only let the worker leave early and weaken the assertion.
        main_snapshot_taken.wait()

    thread = threading.Thread(target=record_active_thread_metrics, daemon=True)
    thread.start()
    try:
        assert worker_ready.wait(timeout=5), (
            "expected the metrics thread to publish its snapshot within 5s"
        )
        main_snapshot = metrics_state.snapshot()
    finally:
        # Release and reap the worker even if the wait or snapshot above fails,
        # so a failing assertion cannot leave the thread parked or unjoined.
        main_snapshot_taken.set()
        thread.join(timeout=5)

    assert not thread.is_alive(), "expected metrics thread to finish"
    assert not snapshot_queue.empty(), (
        "expected the worker to publish exactly one snapshot to the queue"
    )
    active_snapshot = snapshot_queue.get_nowait()

    assert active_snapshot.lock_wait_seconds == pytest.approx(0.5), (
        "the active thread must observe its own accumulated lock wait"
    )
    assert active_snapshot.reentrant_rejection_count == 1, (
        "the active thread must observe its own rejection count"
    )
    assert main_snapshot.lock_wait_seconds == pytest.approx(0.0), (
        "the main thread must not see the worker's lock wait"
    )
    assert main_snapshot.reentrant_rejection_count == 0, (
        "the main thread must not see the worker's rejection count"
    )


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
    metrics_state = _tee_profile_worker_backend._MetricsState(threading.local())
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
    """Nested selector entry raises the dedicated error and bumps the metric."""
    metrics_state = _tee_profile_worker_backend._MetricsState(threading.local())
    selector = _tee_profile_worker_backend._EnvBackendSelector(
        metrics_state=metrics_state
    )
    metrics_state.reset()

    with (
        selector("python"),
        pytest.raises(
            _tee_profile_worker_backend.ReentrantBackendSelectorError
        ) as exc_info,
        selector("auto"),
    ):
        pass

    error = exc_info.value
    metrics = metrics_state.snapshot()
    assert metrics.reentrant_rejection_count >= 1, (
        f"expected at least one reentrant rejection, got {metrics}"
    )
    # RuntimeError ancestry is retained; the structured attributes carry the
    # same rejection count exposed by the selector metrics.
    assert isinstance(error, RuntimeError), (
        "RuntimeError ancestry is retained for backwards compatibility"
    )
    assert error.backend == "auto", "the rejected backend must be recorded on the error"
    assert error.thread_id == threading.get_ident(), (
        "the rejecting thread must be recorded on the error"
    )
    assert error.rejection_count == metrics.reentrant_rejection_count, (
        "the error's rejection count must match the selector metrics"
    )
    assert str(error) == (
        "_EnvBackendSelector is not re-entrant; nested calls are forbidden"
    ), "the error message must name the re-entrancy contract"


def test_repeated_reentrant_rejections_log_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated nested selector failures escalate to an error log."""
    metrics_state = _tee_profile_worker_backend._MetricsState(threading.local())
    selector = _tee_profile_worker_backend._EnvBackendSelector(
        metrics_state=metrics_state
    )
    metrics_state.reset()

    reentrant_error = _tee_profile_worker_backend.ReentrantBackendSelectorError
    with (
        caplog.at_level(logging.ERROR, logger="benchmarks.tee_profile_worker"),
        selector("python"),
    ):
        for _ in range(2):
            with contextlib.suppress(reentrant_error), selector("auto"):
                pass

    error_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR
        and "Repeated re-entrant backend selector rejection" in record.getMessage()
    ]
    assert error_records, "expected repeated reentrant rejection to log an error"
