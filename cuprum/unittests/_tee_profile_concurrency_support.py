"""Scaffolding for tee-profile worker concurrency tests.

These helpers let the concurrent-worker and selector tests drive workers through
precise interleavings: an instrumented re-entrant lock that signals contention,
coordinating backend selectors that expose state transitions, and a race
harness that owns the shared events, observations, and result capture.
"""

from __future__ import annotations

import abc
import contextlib
import dataclasses as dc
import os
import threading
import time
import typing as typ

import pytest
from hypothesis import strategies as st

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth
    import types

    from _pytest.mark.structures import ParameterSet


_EVENT_WAIT_TIMEOUT_SECONDS = 10
_THREAD_JOIN_TIMEOUT_SECONDS = 15


class _RLockLike(typ.Protocol):
    """Structural type for the ``RLock`` operations this test instruments."""

    def acquire(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock, optionally blocking up to ``timeout`` seconds."""
        ...

    def release(self) -> None:
        """Release the lock."""
        ...


class _SignallingRLock:
    """Signal when a blocking acquire observes lock contention."""

    def __init__(
        self,
        delegate: _RLockLike,
        contention_event: threading.Event,
    ) -> None:
        """Store the wrapped lock and contention signal.

        Parameters
        ----------
        delegate
            Lock object that receives all actual acquire and release calls.
        contention_event
            Event set after a blocking acquire attempt observes that the lock is
            already held.
        """
        self._delegate = delegate
        self._contention_event = contention_event

    def acquire(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the wrapped lock, signalling first observed contention.

        Parameters
        ----------
        blocking
            Whether the wrapped acquire may block.
        timeout
            Maximum wait passed to the wrapped lock.

        Returns
        -------
        bool
            The wrapped lock acquisition result.
        """
        if not blocking:
            return self._delegate.acquire(blocking=False)

        if self._delegate.acquire(blocking=False):
            self._delegate.release()
        else:
            self._contention_event.set()

        if timeout == -1:
            return self._delegate.acquire(blocking=True)
        return self._delegate.acquire(blocking=True, timeout=timeout)

    def release(self) -> None:
        """Release the wrapped lock."""
        self._delegate.release()

    def __enter__(self) -> _SignallingRLock:
        """Acquire the wrapped lock on context entry and return self."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        """Release the wrapped lock on context exit."""
        self.release()


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
    """Run one tee-profile worker, synchronizing start via *barrier*.

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
    else:
        with shared.result_lock:
            shared.results.append(result)


def _run_worker_with_selector(
    race: _BackendEnvironmentRace,
    backend: tee_profile_worker.BackendName,
) -> None:
    """Run a worker with selector-internal timing and capture thread failures."""
    try:
        result = run_tee_profile_worker(
            TeeProfileWorkerConfig(
                fixture_path=race.fixture,
                stages=1,
                mode="tee",
                sink_kind="devnull",
                with_line_callbacks=True,
                backend=backend,
                repeat_count=1,
            ),
            backend_selector=race.selector,
        )
        with race.result_lock:
            race.results.append(result)
    except BaseException as exc:  # noqa: BLE001 - thread failures must surface.
        with race.result_lock:
            race.errors.append(exc)


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


def _join_and_assert_finished(
    *threads: threading.Thread,
    context: str = "",
) -> None:
    """Join *threads* and assert all have stopped within the configured timeout.

    The whole group shares one deadline, so the join is bounded by
    ``_THREAD_JOIN_TIMEOUT_SECONDS`` regardless of thread count: each thread is
    joined only for the time remaining until that deadline. Once it passes,
    joining stops and any still-running threads are reported by the assertion
    below (fail fast) rather than each being awaited for a further full timeout.
    """
    deadline = time.monotonic() + _THREAD_JOIN_TIMEOUT_SECONDS
    for thread in threads:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        thread.join(timeout=remaining)
    alive = [thread.name for thread in threads if thread.is_alive()]
    assert not alive, (  # noqa: S101 - test scaffolding assertion
        f"expected threads to finish{f' ({context})' if context else ''}, got {alive}"
    )


def _assert_backend_pair_completes(
    backends: tuple[tee_profile_worker.BackendName, ...],
    fixture: pth.Path,
) -> None:
    """Assert that concurrent workers for *backends* all complete without error.

    Spins up one thread per backend, synchronizes their starts via a
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

    try:
        barrier.wait(timeout=_EVENT_WAIT_TIMEOUT_SECONDS)
    finally:
        # A broken or timed-out barrier must still join the daemon workers so
        # they cannot leak into subsequent tests or Hypothesis examples.
        _join_and_assert_finished(
            *threads, context=f"backend pair completion for {backends}"
        )
    assert not shared.errors, (  # noqa: S101 - test scaffolding assertion
        f"expected no worker thread errors, got {shared.errors!r}"
    )
    assert len(shared.results) == len(backends), (  # noqa: S101 - test scaffolding assertion
        f"expected one result per worker, got {shared.results}"
    )
    assert all(result["status"] == "ok" for result in shared.results), (  # noqa: S101 - test scaffolding assertion
        f"expected all worker statuses to be ok, got {shared.results}"
    )
    assert all(result["exit_code"] == 0 for result in shared.results), (  # noqa: S101 - test scaffolding assertion
        f"expected all worker exit codes to be 0, got {shared.results}"
    )


def _rust_backend_available() -> bool:
    """Report whether the Rust tee-profile backend can run in this environment.

    Consolidates the single private-chain probe into the worker's backend
    module so the callers below share one access point.
    """
    return tee_profile_worker._backend._check_rust_available()


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


class _BaseBackendSelector(abc.ABC):
    """Share coordination state and dispatch for instrumented selectors."""

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
        """Return an instrumented backend-selection context manager."""
        return self._activate(backend)

    @property
    def metrics_state(self) -> tee_profile_worker._MetricsState:
        """Return the delegate metrics state used by worker result assembly."""
        return self._delegate.metrics_state

    @abc.abstractmethod
    @contextlib.contextmanager
    def _activate(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> cabc.Iterator[None]:
        """Coordinate selector behaviour at backend state-machine boundaries."""
        ...  # pragma: no cover


class _CoordinatedBackendSelector(_BaseBackendSelector):
    """Record backend environment values while delegating to the real selector.

    Coordination state and dispatch are inherited from ``_BaseBackendSelector``.
    """

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
                assert self._events["second_selector_attempting"].wait(  # noqa: S101 - test scaffolding assertion
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                ), "expected second_selector_attempting event to signal selector start"
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
            else:
                self._events["second_entered"].set()
            yield


class _CheckpointBackendSelector(_BaseBackendSelector):
    """Expose selector state transitions for interleaving assertions.

    Coordination state and dispatch are inherited from ``_BaseBackendSelector``.
    """

    @contextlib.contextmanager
    def _activate(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> cabc.Iterator[None]:
        """Coordinate lock contention at selector state-machine boundaries."""
        with self._delegate(backend):
            if backend == "python":
                self._events["first_mutated_environment"].set()
                assert self._events["second_waiting_for_lock"].wait(  # noqa: S101 - test scaffolding assertion
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                ), "expected second thread to contend for the backend lock"
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
                assert not self._events["second_entered_context"].is_set(), (  # noqa: S101 - test scaffolding assertion
                    "expected the second thread to block while the first holds the lock"
                )
                did_release = self._events["release_first_context"].wait(
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                )
                assert did_release, (  # noqa: S101 - test scaffolding assertion
                    "expected release_first_context to be signalled before second "
                    "observes env"
                )
            else:
                with self._observation_lock:
                    self._observations.append(os.environ.get("CUPRUM_STREAM_BACKEND"))
                self._events["second_entered_context"].set()
            yield


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
        self.fixture = fixture
        self.selector = _CoordinatedBackendSelector(
            self.events,
            self.observations,
            self.observation_lock,
        )

    def worker_thread(
        self,
        backend: tee_profile_worker.BackendName,
    ) -> threading.Thread:
        """Create a worker thread for the requested backend."""
        return threading.Thread(
            target=_run_worker_with_selector,
            args=(self, backend),
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
