"""Tests for ``benchmarks.tee_profile_worker``.

This module covers the parent-side tee profiling worker at both unit and
integration levels. The tests exercise direct worker calls, CLI invocation, and
the process-wide backend selector used to guard environment mutation.

The suite includes snapshot tests for result structure, concurrency and race
tests around backend selection, reentrancy guard tests, CLI smoke tests, and
parametrised configuration-validation tests.

Key dependencies are ``pytest`` for parametrisation and fixtures, ``syrupy``
for snapshots, and ``threading`` barriers, events, and locks for deterministic
concurrency coordination.
"""

from __future__ import annotations

import contextlib
import dataclasses
import dataclasses as dc
import json
import logging
import os
import subprocess  # noqa: S404 - integration tests exercise fixed CLI commands.
import sys
import threading
import typing as typ

import pytest

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests.conftest import _VOLATILE_KEYS, redact

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


@dataclasses.dataclass
class _WorkerSharedState:
    """Shared mutable state for collecting results and errors from worker threads.

    All access to *results* and *errors* must be performed under *result_lock*
    to prevent data races between concurrent worker threads.
    """

    results: list[tee_profile_worker.TeeProfileWorkerResult] = dataclasses.field(
        default_factory=list,
    )
    errors: list[BaseException] = dataclasses.field(default_factory=list)
    result_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)


def _run_worker_thread(
    backend: tee_profile_worker.BackendName,
    fixture: pth.Path,
    barrier: threading.Barrier,
    shared: _WorkerSharedState,
) -> None:
    """Run one tee-profile worker, synchronising start via *barrier*.

    Appends the result to *shared.results* or the exception to *shared.errors*
    under *shared.result_lock* so that the calling thread can inspect them
    safely after joining.
    """
    try:
        barrier.wait(timeout=5)
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode="tee",
                sink_kind="devnull",
                with_line_callbacks=True,
                backend=backend,
                repeat_count=1,
            ),
        )
    except BaseException as exc:  # noqa: BLE001 - thread failures must surface.
        with shared.result_lock:
            shared.errors.append(exc)
        return

    with shared.result_lock:
        shared.results.append(result)


def _assert_backend_pair_completes(
    backends: tuple[tee_profile_worker.BackendName, ...],
    fixture: pth.Path,
) -> None:
    """Assert that concurrent workers for *backends* all complete without error.

    Spins up one thread per backend, synchronises their starts via a
    ``threading.Barrier``, joins each thread, and asserts that every worker
    returned ``status == "ok"`` and ``exit_code == 0``.
    """
    barrier = threading.Barrier(parties=len(backends) + 1)
    shared = _WorkerSharedState()

    threads = [
        threading.Thread(
            target=_run_worker_thread,
            args=(backend, fixture, barrier, shared),
            daemon=True,
        )
        for backend in backends
    ]

    for thread in threads:
        thread.start()

    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)

    alive_threads = [thread.name for thread in threads if thread.is_alive()]
    assert not alive_threads, (
        f"expected worker threads to finish for {backends}, got {alive_threads}"
    )
    assert not shared.errors, f"expected no worker thread errors, got {shared.errors!r}"
    assert len(shared.results) == len(backends), (
        f"expected one result per worker, got {shared.results}"
    )
    assert all(result["status"] == "ok" for result in shared.results), (
        f"expected all worker statuses to be ok, got {shared.results}"
    )
    assert all(result["exit_code"] == 0 for result in shared.results), (
        f"expected all worker exit codes to be 0, got {shared.results}"
    )


class _CoordinatedBackendSelector:
    """Record backend environment values while delegating to the real selector."""

    def __init__(
        self,
        events: dict[str, threading.Event],
        observations: list[str | None],
        observation_lock: threading.Lock,
    ) -> None:
        """Store shared coordination state for concurrent selector tests."""
        self._events = events
        self._observations = observations
        self._observation_lock = observation_lock
        self._delegate = tee_profile_worker._EnvBackendSelector()

    def __call__(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> contextlib.AbstractContextManager[None]:
        """Return a coordinated backend-selection context manager."""
        return self._activate(backend)

    @contextlib.contextmanager
    def _activate(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> cabc.Iterator[None]:
        """Coordinate worker timing at the selector boundary.

        For non-python backends the method signals *second_selector_attempting*
        before entering the delegate so that the python-backend thread can
        record its environment observation while it still holds ``_BACKEND_LOCK``
        and Thread 2 is about to contend for it. Thread 1 is guaranteed to hold
        the lock when it reads the env because it has not yet exited
        ``with self._delegate("python")``.
        """
        if backend != "python":
            self._events["second_selector_attempting"].set()
        with self._delegate(backend):
            if backend == "python":
                self._events["first_inside"].set()
                assert self._events["second_selector_attempting"].wait(timeout=5), (
                    "expected second_selector_attempting to signal selector contention"
                )
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
            else:
                self._events["second_entered"].set()
            yield


@dc.dataclass(frozen=True, slots=True)
class _SelectorThreadState:
    """Shared state for workers using an injected backend selector."""

    fixture: pth.Path
    backend_selector: tee_profile_worker.BackendSelector
    events: dict[str, threading.Event]
    result_lock: threading.Lock
    results: list[tee_profile_worker.TeeProfileWorkerResult]
    errors: list[BaseException]


class _BackendEnvironmentRace:
    """Own coordination state for the backend environment preservation test."""

    def __init__(self, fixture: pth.Path) -> None:
        """Initialise events, result capture, and the injected selector."""
        self.events = {
            "first_inside": threading.Event(),
            "second_selector_attempting": threading.Event(),
            "second_entered": threading.Event(),
        }
        self.observations: list[str | None] = []
        self.observation_lock = threading.Lock()
        self.results: list[tee_profile_worker.TeeProfileWorkerResult] = []
        self.errors: list[BaseException] = []
        self.result_lock = threading.Lock()
        selector = _CoordinatedBackendSelector(
            self.events,
            self.observations,
            self.observation_lock,
        )
        self.thread_state = _SelectorThreadState(
            fixture,
            selector,
            self.events,
            self.result_lock,
            self.results,
            self.errors,
        )

    def worker_thread(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> threading.Thread:
        """Create a worker thread for the requested backend."""
        return threading.Thread(
            target=_run_worker_with_selector,
            args=(self.thread_state, backend),
            daemon=True,
        )

    def errors_snapshot(self) -> list[BaseException]:
        """Return captured thread errors while holding the result lock."""
        with self.result_lock:
            return list(self.errors)

    def results_snapshot(self) -> list[tee_profile_worker.TeeProfileWorkerResult]:
        """Return captured worker results while holding the result lock."""
        with self.result_lock:
            return list(self.results)

    def observations_snapshot(self) -> list[str | None]:
        """Return recorded environment observations under their lock."""
        with self.observation_lock:
            return list(self.observations)


def _run_worker_with_selector(
    state: _SelectorThreadState,
    backend: tee_profile_worker.BackendName,
) -> None:
    """Run a worker with selector-internal timing and capture thread failures."""
    try:
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=state.fixture,
                stages=1,
                mode="tee",
                sink_kind="devnull",
                with_line_callbacks=True,
                backend=backend,
                repeat_count=1,
            ),
            backend_selector=state.backend_selector,
        )
        with state.result_lock:
            state.results.append(result)
    except BaseException as exc:  # noqa: BLE001 - thread failures must surface.
        with state.result_lock:
            state.errors.append(exc)


_ALTERNATE_BACKEND: tee_profile_worker.BackendName = (
    "rust" if tee_profile_worker._backend._check_rust_available() else "auto"
)

_BACKEND_PAIRS: tuple[
    tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName],
    ...,
] = (
    ("python", "python"),
    ("python", _ALTERNATE_BACKEND),
)


@pytest.mark.parametrize("with_line_callbacks", [False, True])
def test_worker_exercises_parent_side_consume_path(
    tmp_path: pth.Path,
    with_line_callbacks: bool,  # noqa: FBT001 - pytest parametrises this value.
) -> None:
    """A small fixture can run through echo, capture, and tee modes."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    cb_label = "cb" if with_line_callbacks else "nocb"

    for mode in ("echo", "capture", "tee"):
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=fixture,
                stages=1,
                mode=mode,
                sink_kind="devnull",
                with_line_callbacks=with_line_callbacks,
                backend="python",
                repeat_count=1,
            ),
        )

        assert result["status"] == "ok", f"expected worker status ok, got {result}"
        assert result["exit_code"] == 0, (
            f"expected worker exit code 0 for mode {mode}, got {result}"
        )
        assert result["scenario"] == f"{mode}-devnull-{cb_label}-s1-python", (
            f"expected scenario label for mode {mode}, got {result}"
        )
        captured_output_length = result["captured_output_length"]
        if mode == "echo":
            assert captured_output_length == 0, (
                f"expected no captured output in echo mode, got {result}"
            )
        else:
            assert captured_output_length > 0, (
                f"expected captured output in {mode} mode, got {result}"
            )
        stdout_line_count = result["stdout_line_count"]
        if with_line_callbacks:
            assert stdout_line_count > 0, (
                f"expected stdout line callbacks to run, got {result}"
            )
        else:
            assert stdout_line_count == 0, (
                f"expected no stdout line callbacks without callbacks, got {result}"
            )


def test_run_tee_profile_worker_snapshot(
    tmp_path: pth.Path,
    snapshot: SnapshotAssertion,
) -> None:
    """run_tee_profile_worker output structure matches snapshot."""
    fixture = tmp_path / "fixture.b64"
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

    assert redact(result, _VOLATILE_KEYS) == snapshot


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


def test_backend_lock_is_reentrant() -> None:
    """The backend lock can be acquired twice by the same thread."""
    with tee_profile_worker._BACKEND_LOCK:
        # A plain Lock would deadlock on this same-thread second acquisition;
        # RLock tracks ownership and recursion depth, so it succeeds here.
        acquired = tee_profile_worker._BACKEND_LOCK.acquire(
            blocking=True,
            timeout=0.5,
        )
        assert acquired, "expected same-thread backend lock acquisition to succeed"
        tee_profile_worker._BACKEND_LOCK.release()


@pytest.mark.parametrize("backends", _BACKEND_PAIRS)
def test_concurrent_workers_do_not_race(
    tmp_path: pth.Path,
    backends: tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName],
) -> None:
    """Concurrent workers with the given backend pair complete without races."""
    fixture = tmp_path / "fixture_concurrent.b64"
    fixture.write_text("YWJjZGVm\n")
    _assert_backend_pair_completes(backends, fixture)


def test_concurrent_workers_preserve_backend_environment(
    tmp_path: pth.Path,
) -> None:
    """Injected selectors prove concurrent workers preserve backend env state."""
    fixture = tmp_path / "fixture_env_lock.b64"
    fixture.write_text("YWJjZGVm\n")
    race = _BackendEnvironmentRace(fixture)

    first = race.worker_thread("python")
    second = race.worker_thread("auto")
    first.start()
    assert race.events["first_inside"].wait(timeout=5), (
        "expected first worker to enter run body"
    )
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    alive_threads = [thread.name for thread in (first, second) if thread.is_alive()]
    assert not alive_threads, f"expected worker threads to finish, got {alive_threads}"
    assert race.events["second_entered"].is_set(), (
        "expected second worker to enter selector"
    )
    recorded_errors = race.errors_snapshot()
    recorded_results = race.results_snapshot()
    recorded_observations = race.observations_snapshot()
    assert not recorded_errors, (
        f"expected no worker thread errors, got {recorded_errors!r}"
    )
    assert len(recorded_results) == 2, (
        f"expected two worker results, got {recorded_results}"
    )
    assert all(result["status"] == "ok" for result in recorded_results), (
        f"expected all worker statuses to be ok, got {recorded_results}"
    )
    assert all(result["exit_code"] == 0 for result in recorded_results), (
        f"expected all worker exit codes to be 0, got {recorded_results}"
    )
    assert recorded_observations == ["python"], (
        f"expected first worker env to stay pinned, got {recorded_observations}"
    )


def test_nested_selector_raises_runtime_error_and_recovers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Nested backend selector entry fails explicitly and cleans up state."""
    expected_message = (
        "_EnvBackendSelector is not re-entrant; nested calls are forbidden"
    )
    expected_warning = "Rejected re-entrant backend selector activation"
    selector = tee_profile_worker._EnvBackendSelector()
    with selector("python"):  # noqa: SIM117
        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError, match=expected_message):
                with selector("auto"):
                    pass

    assert expected_warning in caplog.text

    with selector("auto"):
        pass


def test_nested_selector_logs_rejection_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reentrant selector activation emits a structured warning log record.

    Verifies that the rejected-backend name, the active-selector flag, and
    a thread-id field are all present in the logged message. The thread_id
    value is redacted for snapshot determinism.
    """
    import re

    selector = tee_profile_worker._EnvBackendSelector()
    with (
        caplog.at_level(
            logging.WARNING,
            logger="benchmarks.tee_profile_worker",
        ),
        selector("python"),
        contextlib.suppress(RuntimeError),
        selector("auto"),
    ):
        pass

    warning_records = [
        record
        for record in caplog.records
        if "re-entrant" in record.getMessage().lower()
    ]
    assert warning_records, "expected a reentrant-rejection warning to be logged"
    msg = warning_records[0].getMessage()
    redacted = re.sub(r"thread_id=\d+", "thread_id=<redacted>", msg)
    assert "backend='auto'" in redacted, (
        f"expected rejected backend name in warning, got: {redacted!r}"
    )
    assert "thread_id=<redacted>" in redacted, (
        f"expected thread_id field in warning, got: {redacted!r}"
    )
    assert "selector_active=True" in redacted, (
        f"expected selector_active field in warning, got: {redacted!r}"
    )


def test_worker_cli_reports_config_errors(
    tmp_path: pth.Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Worker CLI returns a process-style error for invalid configuration."""
    missing_fixture = tmp_path / "missing.b64"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tee_profile_worker.py",
            "--fixture",
            str(missing_fixture),
            "--stages",
            "1",
            "--mode",
            "echo",
            "--sink-kind",
            "devnull",
        ],
    )

    assert tee_profile_worker.main() == 2, "expected invalid worker config exit code 2"
    captured = capsys.readouterr()
    expected_error = f"fixture_path must exist and be a file: {missing_fixture}"
    assert expected_error in captured.err, (
        f"expected missing fixture error on stderr, got {captured.err!r}"
    )


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        pytest.param(
            {"stages": 0},
            "stages must be >= 1",
            id="stages-zero",
        ),
        pytest.param(
            {"repeat_count": 0},
            "repeat-count must be >= 1",
            id="repeat-count-zero",
        ),
        pytest.param(
            {"mode": "invalid"},
            "mode must be one of",
            id="invalid-mode",
        ),
        pytest.param(
            {"sink_kind": "invalid"},
            "sink-kind must be one of",
            id="invalid-sink",
        ),
        pytest.param(
            {"backend": "invalid"},
            "backend must be one of",
            id="invalid-backend",
        ),
    ],
)
def test_worker_config_rejects_invalid_fields(
    tmp_path: pth.Path,
    kwargs: dict[str, object],
    fragment: str,
) -> None:
    """TeeProfileWorkerConfig raises ValueError for invalid field values."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJj\n")
    base: dict[str, object] = {
        "fixture_path": fixture,
        "stages": 1,
        "mode": "echo",
        "sink_kind": "devnull",
        "with_line_callbacks": False,
        "backend": "python",
        "repeat_count": 1,
    }
    base.update(kwargs)
    config_type = typ.cast("typ.Any", TeeProfileWorkerConfig)
    with pytest.raises(ValueError, match=fragment):
        config_type(**base)


def test_worker_cli_runs_successfully_via_subprocess(tmp_path: pth.Path) -> None:
    """Worker CLI produces a valid JSON result when invoked as a subprocess."""
    fixture = tmp_path / "fixture.b64"
    fixture.write_text("YWJjZGVm\n")
    output = tmp_path / "result.json"
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "benchmarks.tee_profile_worker",
            "--fixture",
            str(fixture),
            "--stages",
            "1",
            "--mode",
            "echo",
            "--sink-kind",
            "devnull",
            "--backend",
            "python",
            "--repeat-count",
            "1",
            "--output",
            str(output),
        ],
        check=False,
    )

    assert completed.returncode == 0, f"expected exit 0, got {completed.returncode}"
    assert output.exists(), "expected worker-result.json to be written"
    result = json.loads(output.read_text())
    assert result["status"] == "ok", f"expected worker status ok, got {result}"
    assert result["exit_code"] == 0, f"expected worker exit code 0, got {result}"
