"""Tests that concurrent ``run_tee_profile_worker`` calls across backend pairs complete without races."""  # noqa: E501

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings

from cuprum.unittests._tee_profile_worker_test_helpers import (
    _assert_backend_pair_completes,
    _backend_lists,
    _backend_pairs,
)

if typ.TYPE_CHECKING:
    import pathlib as pth

    from benchmarks import tee_profile_worker


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
