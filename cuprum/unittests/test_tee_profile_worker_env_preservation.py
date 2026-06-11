"""Backend-environment isolation and lock-serialisation tests.

These tests inject coordinating backend selectors so concurrent workers can be
driven through precise interleavings, proving that ``_BACKEND_LOCK`` serialises
backend-environment mutation and that each worker observes only its own pinned
``CUPRUM_STREAM_BACKEND`` value.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import os
import threading
import typing as typ

from benchmarks import tee_profile_worker
from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests.conftest import (
    _EVENT_WAIT_TIMEOUT_SECONDS,
    _join_and_assert_finished,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib as pth
    import types

    import pytest


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
        if blocking and timeout == -1:
            if self._delegate.acquire(blocking=False):
                self._delegate.release()
            else:
                self._contention_event.set()
        if timeout == -1:
            return self._delegate.acquire(blocking=blocking)
        return self._delegate.acquire(blocking=blocking, timeout=timeout)

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


@dc.dataclass(frozen=True, slots=True)
class _SelectorThreadState:
    """Shared state for workers using an injected backend selector."""

    fixture: pth.Path
    backend_selector: tee_profile_worker.BackendSelector
    events: dict[str, threading.Event]
    result_lock: threading.Lock
    results: list[tee_profile_worker.TeeProfileWorkerResult]
    errors: list[BaseException]


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
                did_release = self._events["release_first_context"].wait(
                    timeout=_EVENT_WAIT_TIMEOUT_SECONDS,
                )
                assert did_release, (
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
    _join_and_assert_finished(
        first,
        second,
        context="backend environment preservation",
    )

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


def test_selector_interleaving_blocks_environment_observation_until_unlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    monkeypatch.setattr(
        tee_profile_worker,
        "_BACKEND_LOCK",
        _SignallingRLock(
            tee_profile_worker._BACKEND_LOCK,
            events["second_waiting_for_lock"],
        ),
    )
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
    _join_and_assert_finished(
        first,
        second,
        context="selector interleaving",
    )
    assert not errors, f"expected no selector thread errors, got {errors!r}"
    with observation_lock:
        assert observations == ["python", None], (
            f"expected serialised backend observations, got {observations}"
        )
