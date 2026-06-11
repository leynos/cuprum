"""Backend-environment isolation and lock-serialisation tests.

These tests inject coordinating backend selectors so concurrent workers can be
driven through precise interleavings, proving that ``_BACKEND_LOCK`` serialises
backend-environment mutation and that each worker observes only its own pinned
``CUPRUM_STREAM_BACKEND`` value.
"""

from __future__ import annotations

import threading
import typing as typ

from benchmarks import tee_profile_worker
from cuprum.unittests._tee_profile_worker_test_helpers import (
    _BackendEnvironmentRace,
    _CheckpointBackendSelector,
    _SignallingRLock,
)
from cuprum.unittests.conftest import (
    _EVENT_WAIT_TIMEOUT_SECONDS,
    _join_and_assert_finished,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    import pytest


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
