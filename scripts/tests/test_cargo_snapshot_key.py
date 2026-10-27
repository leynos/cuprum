"""Check Cargo snapshot keys against the Actions cache dimensions."""

from __future__ import annotations

import hashlib
import json
import typing as typ

import pytest

from scripts import cargo_state
from scripts.cargo_state_metadata import _RunnerScope
from scripts.tests.cargo_state_support import create_repository

if typ.TYPE_CHECKING:
    from pathlib import Path

_GITHUB_HOSTED = _RunnerScope("Linux", "X64", "github-hosted", "v1")
_SELF_HOSTED = _RunnerScope("Linux", "X64", "self-hosted", "v1")


def test_phase_records_are_json_lines_with_monotonic_durations(tmp_path: Path) -> None:
    """The run log stores phase names and seconds without environment values."""
    timings_path = tmp_path / "timings.jsonl"
    cargo_state.record_phase(timings_path, "dependency_preparation", 1.25)
    cargo_state.record_phase(timings_path, "rust_test_workload", 3.5)

    records = [
        json.loads(line)
        for line in timings_path.read_text(encoding="utf-8").splitlines()
    ]

    assert len(records) == 2, "each phase must produce one JSON-line record"
    preparation, workload = records
    assert preparation["phase"] == "dependency_preparation", (
        "the first record must name dependency preparation"
    )
    assert preparation["seconds"] == pytest.approx(1.25), (
        "the preparation duration must match its monotonic sample"
    )
    assert workload["phase"] == "rust_test_workload", (
        "the second record must name the test workload"
    )
    assert workload["seconds"] == pytest.approx(3.5), (
        "the workload duration must match its monotonic sample"
    )


def test_cache_key_tracks_action_inputs_cargo_version_and_runner_scope(
    tmp_path: Path,
) -> None:
    """The cache key changes with every input that shapes Cargo state."""
    repository = create_repository(tmp_path / "repo")
    baseline = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.85.0",
        runner=_GITHUB_HOSTED,
    )

    (repository / "rust/Cargo.lock").write_text(
        "version = 4\n# changed\n", encoding="utf-8"
    )
    changed_lock = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.85.0",
        runner=_GITHUB_HOSTED,
    )
    (repository / "rust/Cargo.lock").write_text("version = 4\n", encoding="utf-8")
    (repository / "rust/rust-toolchain.toml").write_text(
        "[toolchain]\nchannel = '1.86.0'\n", encoding="utf-8"
    )
    changed_toolchain = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.85.0",
        runner=_GITHUB_HOSTED,
    )
    changed_cargo = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.86.0",
        runner=_GITHUB_HOSTED,
    )
    changed_scope = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.85.0",
        runner=_SELF_HOSTED,
    )

    assert changed_lock != baseline, "Cargo.lock changes must invalidate the snapshot"
    assert changed_toolchain != baseline, (
        "toolchain changes must invalidate the snapshot"
    )
    assert changed_cargo != changed_toolchain, "Cargo versions need distinct snapshots"
    assert changed_scope != baseline, "runner environment must scope cache snapshots"
    assert baseline.startswith("cargo-v1-Linux-X64-github-hosted-"), (
        "the scope must match the cache action generation and runner fields"
    )


def test_cache_content_digest_matches_the_action_file_hash_shape(
    tmp_path: Path,
) -> None:
    """The lock and toolchain bytes feed the same nested SHA-256 shape."""
    repository = create_repository(tmp_path / "repo")
    expected_inputs = [
        (repository / "rust/Cargo.lock").read_bytes(),
        (repository / "rust/rust-toolchain.toml").read_bytes(),
    ]
    file_hashes = [
        hashlib.sha256(data).hexdigest().encode() for data in expected_inputs
    ]
    file_hashes.append(hashlib.sha256(b"cargo 1.85.0").hexdigest().encode())
    expected_digest = hashlib.sha256(b"".join(file_hashes)).hexdigest()

    key = cargo_state.cargo_snapshot_key(
        repository,
        "cargo 1.85.0",
        runner=_GITHUB_HOSTED,
    )

    assert key.endswith(expected_digest), (
        "the content address must hash each input before the digest sequence"
    )
