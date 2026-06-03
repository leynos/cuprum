"""Concurrency and reentrancy tests for ``_EnvBackendSelector``.

The ``_EnvBackendSelector`` concurrency tests mirror the state transitions a
threading-level model checker would verify:

1. ``_BACKEND_LOCK`` is held for the full context duration.
2. ``os.environ["CUPRUM_STREAM_BACKEND"]`` is restored to its previous value on
   exit.
3. Backend availability and dispatch LRU caches are cleared on entry and exit.
4. Same-thread reentrancy is rejected before nested environment mutation.

Candidate full model-checking routes include ``pynusmv`` and translating the
selector state machine to Promela for SPIN. Full tool integration is out of
scope for this ticket; the explicit checkpoint tests below keep the observable
states aligned with the model such tools would explore.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import os
import re
import threading
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth

    from syrupy.assertion import SnapshotAssertion


_EVENT_WAIT_TIMEOUT_SECONDS = 10
_THREAD_JOIN_TIMEOUT_SECONDS = 15


@dc.dataclass(slots=True)
class _WorkerSharedState:
    """Shared mutable state for collecting results and errors from worker threads.

    All access to *results* and *errors* must be performed under *result_lock*
    to prevent data races between concurrent worker threads.
    """

    results: list[tee_profile_worker.TeeProfileWorkerResult] = dc.field(
        default_factory=list,
    )
    errors: list[BaseException] = dc.field(default_factory=list)
    result_lock: threading.Lock = dc.field(default_factory=threading.Lock)


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
        barrier.wait(timeout=_EVENT_WAIT_TIMEOUT_SECONDS)
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


def _available_backend_names() -> tuple[tee_profile_worker.BackendName, ...]:
    """Return backend names that can run in this environment."""
    if tee_profile_worker._backend._check_rust_available():
        return ("auto", "python", "rust")
    return ("auto", "python")


@st.composite
def _backend_lists(
    draw: st.DrawFn,
) -> tuple[tee_profile_worker.BackendName, ...]:
    """Generate same-length backend sequences for concurrent worker tests."""
    thread_count = draw(st.integers(min_value=2, max_value=8))
    backend = st.sampled_from(_available_backend_names())
    return tuple(draw(st.lists(backend, min_size=thread_count, max_size=thread_count)))


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

    barrier.wait(timeout=_EVENT_WAIT_TIMEOUT_SECONDS)
    for thread in threads:
        thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

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
                assert self._events["second_selector_attempting"].wait(
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                ), "expected second_selector_attempting event to signal selector start"
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
            else:
                self._events["second_entered"].set()
            yield


class _CheckpointBackendSelector:
    """Expose selector state transitions for interleaving assertions."""

    def __init__(
        self,
        events: dict[str, threading.Event],
        observations: list[str | None],
        observation_lock: threading.Lock,
    ) -> None:
        """Store interleaving checkpoints and observed backend env values."""
        self._events = events
        self._observations = observations
        self._observation_lock = observation_lock
        self._delegate = tee_profile_worker._EnvBackendSelector()

    def __call__(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> contextlib.AbstractContextManager[None]:
        """Return a checkpointed backend-selection context manager."""
        return self._activate(backend)

    @contextlib.contextmanager
    def _activate(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> cabc.Iterator[None]:
        """Coordinate lock contention at selector state-machine boundaries."""
        if backend != "python":
            self._events["second_waiting_for_lock"].set()
        with self._delegate(backend):
            if backend == "python":
                self._events["first_mutated_environment"].set()
                assert self._events["second_waiting_for_lock"].wait(
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                ), "expected second thread to contend for the backend lock"
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
                assert not self._events["second_entered_context"].is_set(), (
                    "expected the second thread to block while the first holds the lock"
                )
                self._events["release_first_context"].wait(
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                )
            else:
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
                self._events["second_entered_context"].set()
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


def _run_selector_context(
    selector: tee_profile_worker.BackendSelector,
    backend: tee_profile_worker.BackendName,
    errors: list[BaseException],
    result_lock: threading.Lock,
) -> None:
    """Enter a selector context and capture thread failures."""
    try:
        with selector(backend):
            pass
    except BaseException as exc:  # noqa: BLE001 - thread failures must surface.
        with result_lock:
            errors.append(exc)


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


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    outer_backend=st.sampled_from(_available_backend_names()),
    inner_backend=st.sampled_from(_available_backend_names()),
)
def test_nested_selector_rejects_generated_backend_pairs(
    outer_backend: tee_profile_worker.BackendName,
    inner_backend: tee_profile_worker.BackendName,
) -> None:
    """Same-thread nested selector entry always raises before mutation."""
    selector = tee_profile_worker._EnvBackendSelector()
    with (
        selector(outer_backend),
        pytest.raises(
            RuntimeError,
            match=tee_profile_worker._REENTRANT_SELECTOR_MESSAGE,
        ),
        selector(inner_backend),
    ):
        pass


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
    snapshot: SnapshotAssertion,
) -> None:
    """Reentrant selector activation emits a structured warning log record.

    Verifies that the rejected-backend name, the active-selector flag, and
    a thread-id field are all present in the logged message. The thread_id
    value is redacted for snapshot determinism.
    """
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
    assert redacted == snapshot, (
        f"Snapshot mismatch: redacted={redacted!r} expected={snapshot!r}"
    )


@pytest.mark.parametrize("backends", _BACKEND_PAIRS)
def test_concurrent_workers_do_not_race(
    tmp_path: pth.Path,
    backends: tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName],
) -> None:
    """Concurrent workers with the given backend pair complete without races."""
    fixture = tmp_path / "fixture_concurrent.b64"
    fixture.write_text("YWJjZGVm\n")
    _assert_backend_pair_completes(backends, fixture)


@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(backends=_backend_lists())
def test_generated_concurrent_workers_complete(
    tmp_path: pth.Path,
    backends: tuple[tee_profile_worker.BackendName, ...],
) -> None:
    """Generated concurrent backend selections all complete successfully."""
    fixture = tmp_path / "fixture_generated_concurrent.b64"
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
    assert race.events["first_inside"].wait(timeout=_EVENT_WAIT_TIMEOUT_SECONDS), (
        "expected first worker to enter run body"
    )
    second.start()
    first.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    second.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

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


def test_selector_interleaving_blocks_environment_observation_until_unlock() -> None:
    """Checkpointed interleaving proves lock serialisation of env mutation."""
    events = {
        "first_mutated_environment": threading.Event(),
        "second_waiting_for_lock": threading.Event(),
        "second_entered_context": threading.Event(),
        "release_first_context": threading.Event(),
    }
    observations: list[str | None] = []
    observation_lock = threading.Lock()
    errors: list[BaseException] = []
    result_lock = threading.Lock()
    selector = _CheckpointBackendSelector(events, observations, observation_lock)
    first = threading.Thread(
        target=_run_selector_context,
        args=(selector, "python", errors, result_lock),
        daemon=True,
    )
    second = threading.Thread(
        target=_run_selector_context,
        args=(selector, "auto", errors, result_lock),
        daemon=True,
    )

    first.start()
    assert events["first_mutated_environment"].wait(
        timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
    ), "expected first thread to mutate the backend environment"
    second.start()
    assert events["second_waiting_for_lock"].wait(
        timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
    ), "expected second thread to reach the lock boundary"
    assert not events["second_entered_context"].is_set(), (
        "expected second thread to remain outside the context while locked"
    )
    events["release_first_context"].set()
    first.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    second.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)

    alive_threads = [thread.name for thread in (first, second) if thread.is_alive()]
    assert not alive_threads, (
        f"expected selector threads to finish, got {alive_threads}"
    )
    assert not errors, f"expected no selector thread errors, got {errors!r}"
    with observation_lock:
        assert observations == ["python", None], (
            f"expected serialised backend observations, got {observations}"
        )
