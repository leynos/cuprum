"""Exercise the ``prepare_cargo_state`` orchestration and its CLI contract."""

from __future__ import annotations

import json
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the CLI contract is a real process invocation.
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import prepare_cargo_state
from scripts.tests.cargo_state_support import create_repository

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CLI_TIMEOUT_SECONDS = 20
STUB_CARGO_VERSION = "cargo 1.85.0"


def _run_records(run_root: Path) -> list[dict[str, str | float]]:
    """Parse the run's JSON-lines timing records."""
    return [
        json.loads(line)
        for line in (run_root / "timings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


class _PreparationRecorder:
    """Check that one preparation receives and reports the controlled run."""

    def __init__(
        self,
        *,
        run_root: Path,
        repository: Path,
        snapshot_root: Path,
        caller_home: Path,
    ) -> None:
        """Store the controlled roots and the caller's Cargo configuration."""
        self.run_root = run_root
        self.repository = repository
        self.snapshot_root = snapshot_root
        self.caller_home = caller_home
        self.calls: list[str] = []
        self.prepared = SimpleNamespace(
            path=snapshot_root / "snapshot-cache",
            key="key-0001",
            cargo_version=STUB_CARGO_VERSION,
            source_cargo_home=caller_home,
            reused=True,
        )

    def prepare_snapshot(
        self,
        repository_root: Path,
        requested_root: Path,
        *,
        context: object,
    ) -> SimpleNamespace:
        """Check the requested roots and report the prepared snapshot."""
        assert (repository_root, requested_root) == (
            self.repository,
            self.snapshot_root,
        ), "snapshot preparation must receive the requested repository and root"
        assert context == prepare_cargo_state.PreparationContext(
            diagnostics_path=self.run_root / "cargo-diagnostics.jsonl"
        ), "preparation must retain its diagnostics in the run root"
        self.calls.append("snapshot")
        return self.prepared

    def create_home(
        self,
        snapshot_path: Path,
        requested_run_root: Path,
        *,
        source_cargo_home: Path,
    ) -> Path:
        """Check the copied snapshot and report the run home path."""
        assert snapshot_path == self.prepared.path, (
            "the run home must copy the snapshot preparation published"
        )
        assert requested_run_root == self.run_root, (
            "the run home must live in the run root the caller named"
        )
        assert source_cargo_home == self.caller_home, (
            "the run home must copy the caller's Cargo configuration"
        )
        self.calls.append("run-home")
        return requested_run_root / "cargo-home"


def _assert_reported_metadata(
    metadata: dict[str, object],
    record: dict[str, str | float],
    run_root: Path,
) -> None:
    """Check the report names the run-local home and the recorded cost."""
    assert metadata["cargo_home"] == str(run_root / "cargo-home"), (
        "the reported home must be the run-local one"
    )
    assert metadata["snapshot_key"] == "key-0001", (
        "the report must name the snapshot the run copied"
    )
    assert metadata["cargo_version"] == STUB_CARGO_VERSION, (
        "the report must name the Cargo version that prepared the snapshot"
    )
    assert metadata["preparation_seconds"] == record["seconds"], (
        "the reported cost must match the recorded preparation phase"
    )
    assert metadata["snapshot_reused"] is True, (
        "the report must disclose that the snapshot was reused"
    )


def test_main_prepares_before_creating_the_run_home_and_reports_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Snapshot preparation precedes run-home creation and both are reported."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    recorder = _PreparationRecorder(
        run_root=run_root,
        repository=tmp_path / "repository",
        snapshot_root=tmp_path / "snapshots",
        caller_home=tmp_path / "caller-cargo-home",
    )
    monkeypatch.setattr(
        prepare_cargo_state, "prepare_cargo_snapshot", recorder.prepare_snapshot
    )
    monkeypatch.setattr(
        prepare_cargo_state, "create_run_cargo_home", recorder.create_home
    )

    prepare_cargo_state.main(run_root, recorder.repository, recorder.snapshot_root)

    assert recorder.calls == ["snapshot", "run-home"], (
        "the run home must be copied after the snapshot is published"
    )
    [record] = _run_records(run_root)
    assert record["phase"] == "dependency_preparation", (
        "preparation must be attributable apart from the workload"
    )
    assert float(record["seconds"]) >= 0, (
        "the CLI must record the preparation cost it reports"
    )
    _assert_reported_metadata(json.loads(capsys.readouterr().out), record, run_root)


def _write_stub_cargo(bin_directory: Path) -> Path:
    """Create a Cargo stand-in that reports a version and fetches nothing."""
    stub = bin_directory / "cargo"
    bin_directory.mkdir(parents=True, exist_ok=True)
    stub.write_text(
        "\n".join((
            "#!/bin/sh",
            'if [ "$1" = "--version" ]; then',
            f"  echo '{STUB_CARGO_VERSION}'",
            "fi",
            "exit 0",
            "",
        )),
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def test_cli_prepares_a_run_home_and_records_the_preparation(tmp_path: Path) -> None:
    """The invoked CLI reports non-secret metadata for a usable run home."""
    repository = create_repository(tmp_path / "repository")
    run_root = tmp_path / "run"
    run_root.mkdir()
    snapshot_root = tmp_path / "snapshots"
    stub_cargo = _write_stub_cargo(tmp_path / "bin")
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "home"),
        "CARGO": str(stub_cargo),
        "CARGO_HOME": str(tmp_path / "absent-caller-cargo-home"),
        "RUSTUP_HOME": str(tmp_path / "absent-rustup-home"),
    }

    completed = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - a fixed argv verifies the module entry point.
        [
            sys.executable,
            "-m",
            "scripts.prepare_cargo_state",
            str(run_root),
            "--repository-root",
            str(repository),
            "--snapshot-root",
            str(snapshot_root),
        ],
        capture_output=True,
        check=False,
        cwd=REPOSITORY_ROOT,
        env=environment,
        text=True,
        timeout=CLI_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr
    metadata = json.loads(completed.stdout)
    run_home = run_root / "cargo-home"
    assert metadata["cargo_home"] == str(run_home), (
        "the reported home must be the run-local one"
    )
    assert run_home.is_dir(), "the workload needs the run-local Cargo home to exist"
    assert metadata["cargo_version"] == STUB_CARGO_VERSION, (
        "the report must name the Cargo version that prepared the snapshot"
    )
    assert metadata["snapshot_reused"] is False, (
        "a first run must report that it published a new snapshot"
    )
    [record] = _run_records(run_root)
    assert record["phase"] == "dependency_preparation", (
        "the CLI must separate preparation cost from the later workload"
    )
    assert float(record["seconds"]) == pytest.approx(metadata["preparation_seconds"]), (
        "the JSON report must repeat the value the timings file records"
    )
    recorded_operations = [
        json.loads(line)["operation"]
        for line in (run_root / "cargo-diagnostics.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert recorded_operations == ["cargo version", "cargo fetch --locked"], (
        "the CLI must retain the preparation invocations it ran"
    )
