"""Concurrent-worker and environment-preservation tee-profile worker tests.

These tests spin up multiple tee-profile workers across the available backends
and drive coordinating backend selectors through precise interleavings.
"""

from __future__ import annotations

import threading
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings

from benchmarks import tee_profile_worker
from cuprum.unittests._tee_profile_backend_support import (
    _backend_lists,
    _backend_pairs,
)
from cuprum.unittests._tee_profile_concurrency_support import (
    _EVENT_WAIT_TIMEOUT_SECONDS,
    _assert_backend_pair_completes,
    _BackendEnvironmentRace,
    _CheckpointBackendSelector,
    _join_and_assert_finished,
    _run_selector_context,
)
from cuprum.unittests._tee_profile_signalling_lock import _SignallingRLock

if typ.TYPE_CHECKING:
    import pathlib as pth


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
