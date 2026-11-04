"""Exercise boundary Cargo run cleanup, timing, and diagnostics placement."""

from __future__ import annotations

import json
import typing as typ
from types import SimpleNamespace

import pytest

from scripts import boundary_cargo
from scripts.boundary_cargo import boundary_cargo_run, prepare_run_cargo_home
from scripts.cargo_diagnostics import DIAGNOSTICS_FILE_NAME
from scripts.cargo_preparation import PreparationContext

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class _WorkloadError(RuntimeError):
    """Stand in for a workload that fails after preparation succeeds."""


class _PreparationError(RuntimeError):
    """Stand in for preparation that fails before the workload starts."""


def _phases(run_root: Path) -> list[str]:
    """Return the timing phases one run recorded, in order."""
    return [
        json.loads(line)["phase"]
        for line in (run_root / "timings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _create_owned_resources(run_root: Path) -> None:
    """Create the Cargo home and target directory a run owns."""
    (run_root / "cargo-home/registry").mkdir(parents=True)
    (run_root / "target/debug").mkdir(parents=True)


def _creating_preparation(
    workspace: Path,
    seen: list[Path],
) -> cabc.Callable[[Path, Path], float]:
    """Return a preparation callback that creates the run's build resources."""

    def prepare(run_root: Path, prepared_workspace: Path) -> float:
        """Create the owned resources and report a measurable duration."""
        assert prepared_workspace == workspace, (
            "preparation must receive the workspace under test"
        )
        _create_owned_resources(run_root)
        seen.append(run_root)
        return 0.25

    return prepare


def test_successful_run_records_both_phases_and_removes_owned_state(
    tmp_path: Path,
) -> None:
    """A completed run keeps its timings but no Cargo home or target directory."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    workspace = tmp_path / "workspace"
    seen: list[Path] = []

    with boundary_cargo_run(
        run_root, workspace, _creating_preparation(workspace, seen)
    ):
        assert seen == [run_root]
        assert (run_root / "cargo-home").is_dir(), (
            "the workload must run against the prepared Cargo home"
        )
        assert (run_root / "target").is_dir(), (
            "the workload must run against the prepared target directory"
        )

    assert _phases(run_root) == ["dependency_preparation", "workload"], (
        "preparation and workload costs must stay separately attributable"
    )
    assert (run_root / "timings.jsonl").is_file(), (
        "the run root must survive cleanup so its timings remain readable"
    )
    assert not (run_root / "cargo-home").exists(), (
        "an owned Cargo home must not outlive its run"
    )
    assert not (run_root / "target").exists(), (
        "an owned target directory must not outlive its run"
    )


def test_workload_failure_records_both_phases_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    """A failing workload propagates while its timing and cleanup still run."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    workspace = tmp_path / "workspace"
    seen: list[Path] = []

    with (
        pytest.raises(_WorkloadError),
        boundary_cargo_run(run_root, workspace, _creating_preparation(workspace, seen)),
    ):
        raise _WorkloadError

    assert _phases(run_root) == ["dependency_preparation", "workload"], (
        "a failed workload must still leave a measurable workload phase"
    )
    assert not (run_root / "cargo-home").exists()
    assert not (run_root / "target").exists()


def test_preparation_failure_records_preparation_and_still_cleans_up(
    tmp_path: Path,
) -> None:
    """A failing preparation is timed, propagates, and leaves no build state."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    workspace = tmp_path / "workspace"

    def prepare(failed_root: Path, _workspace: Path) -> float:
        """Create the owned resources and then fail before the workload."""
        _create_owned_resources(failed_root)
        raise _PreparationError

    with (
        pytest.raises(_PreparationError),
        boundary_cargo_run(run_root, workspace, prepare),
    ):
        pytest.fail("the workload must not run when preparation failed")

    assert _phases(run_root) == ["dependency_preparation"], (
        "a preparation failure must still record the time it consumed"
    )
    assert not (run_root / "cargo-home").exists()
    assert not (run_root / "target").exists()


def test_run_home_preparation_retains_diagnostics_beside_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preparation names the run-local diagnostics file for every Cargo call."""
    repository = tmp_path / "repository"
    run_root = tmp_path / "run"
    workspace = tmp_path / "workspace"
    snapshot_root = tmp_path / "snapshots"
    snapshot = SimpleNamespace(
        path=tmp_path / "snapshot-cache",
        source_cargo_home=tmp_path / "caller-cargo-home",
    )
    observed: dict[str, object] = {}

    def prepare_snapshot(
        repository_root: Path,
        requested_root: Path,
        *,
        context: PreparationContext,
    ) -> SimpleNamespace:
        """Record snapshot preparation and report a published snapshot."""
        observed["snapshot"] = (repository_root, requested_root, context)
        return snapshot

    def create_home(
        snapshot_path: Path,
        requested_run_root: Path,
        *,
        source_cargo_home: Path,
    ) -> Path:
        """Record run-home creation and report the home path."""
        observed["run_home"] = (snapshot_path, requested_run_root, source_cargo_home)
        return requested_run_root / "cargo-home"

    def fetch_dependencies(
        manifest: Path,
        cargo_home: Path,
        *,
        context: PreparationContext,
    ) -> float:
        """Record dependency preparation for the copied workspace."""
        observed["fetch"] = (manifest, cargo_home, context)
        return 0.0

    monkeypatch.setattr(boundary_cargo, "prepare_cargo_snapshot", prepare_snapshot)
    monkeypatch.setattr(boundary_cargo, "create_run_cargo_home", create_home)
    monkeypatch.setattr(
        boundary_cargo, "fetch_workspace_dependencies", fetch_dependencies
    )
    monkeypatch.setattr(boundary_cargo, "default_snapshot_root", lambda: snapshot_root)

    elapsed = prepare_run_cargo_home(repository, run_root, workspace)

    context = PreparationContext(diagnostics_path=run_root / DIAGNOSTICS_FILE_NAME)
    assert observed["snapshot"] == (repository, snapshot_root, context), (
        "snapshot preparation must retain diagnostics in the run root"
    )
    assert observed["run_home"] == (
        snapshot.path,
        run_root,
        snapshot.source_cargo_home,
    ), "the run home must copy the published snapshot"
    assert observed["fetch"] == (
        workspace / "Cargo.toml",
        run_root / "cargo-home",
        context,
    ), "the copied workspace's locked dependencies must be fetched"
    assert elapsed >= 0, "preparation must report its own elapsed time"
