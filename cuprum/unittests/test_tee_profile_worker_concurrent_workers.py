"""Concurrent-worker race-freedom tests for the tee profiling worker.

These tests spin up multiple tee-profile workers across the available backends
and assert that they all complete successfully when run concurrently, covering
both fixed backend pairs and Hypothesis-generated backend sequences.
"""

from __future__ import annotations

import dataclasses as dc
import threading
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings

from benchmarks.tee_profile_worker import TeeProfileWorkerConfig, run_tee_profile_worker
from cuprum.unittests.conftest import (
    _EVENT_WAIT_TIMEOUT_SECONDS,
    _alternate_backend,
    _backend_lists,
    _join_and_assert_finished,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from benchmarks import tee_profile_worker
    from _pytest.mark.structures import ParameterSet


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


def _backend_pairs() -> tuple[
    tuple[tee_profile_worker.BackendName, tee_profile_worker.BackendName]
    | ParameterSet,
    ...,
]:
    """Return parametrised backend pairs for concurrent-worker tests."""
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

    try:
        barrier.wait(timeout=_EVENT_WAIT_TIMEOUT_SECONDS)
    finally:
        # A broken or timed-out barrier must still join the daemon workers so
        # they cannot leak into subsequent tests or Hypothesis examples.
        _join_and_assert_finished(
            *threads, context=f"backend pair completion for {backends}"
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


@pytest.mark.parametrize("backends", _backend_pairs())
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
