"""Check isolated workspace fetches and independent writable Cargo homes."""

from __future__ import annotations

import errno
import stat
import typing as typ
from types import SimpleNamespace

import pytest

from scripts import cargo_run_home, cargo_state
from scripts.tests.cargo_state_support import create_repository

if typ.TYPE_CHECKING:
    from pathlib import Path


def test_run_homes_receive_current_caller_settings_but_never_credentials(
    tmp_path: Path,
) -> None:
    """Each run captures current settings; a snapshot never supplies them."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "config.toml").write_text("[net]\noffline = true\n", encoding="utf-8")
    source_home = tmp_path / "caller-cargo-home"
    source_home.mkdir()
    first_settings = "[net]\noffline = false\n"
    (source_home / "config.toml").write_text(first_settings, encoding="utf-8")
    (source_home / "credentials.toml").write_text(
        "token = 'private-token'\n", encoding="utf-8"
    )

    first = cargo_state.create_run_cargo_home(
        snapshot,
        tmp_path / "run-one",
        source_cargo_home=source_home,
    )
    second_settings = "[build]\nrustc-wrapper = 'build-limits-rustc'\n"
    (source_home / "config.toml").write_text(second_settings, encoding="utf-8")
    second = cargo_state.create_run_cargo_home(
        snapshot,
        tmp_path / "run-two",
        source_cargo_home=source_home,
    )

    assert (first / "config.toml").read_text(encoding="utf-8") == first_settings, (
        "a run home must receive the caller's settings, not a stale snapshot copy"
    )
    assert (second / "config.toml").read_text(encoding="utf-8") == second_settings, (
        "a later run must see configuration changed after the snapshot was made"
    )
    assert not (first / "credentials.toml").exists(), (
        "a run home must never receive caller credentials"
    )


def test_a_snapshot_credential_is_removed_from_the_run_home(tmp_path: Path) -> None:
    """An older snapshot carrying credentials must not authenticate a run."""
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "credentials.toml").write_text(
        "token = 'stale-token'\n", encoding="utf-8"
    )

    run_home = cargo_state.create_run_cargo_home(snapshot, tmp_path / "run")

    assert not (run_home / "credentials.toml").exists(), (
        "a run home must never retain credentials carried by an older snapshot"
    )


def test_copied_workspace_dependencies_are_fetched_locked_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An isolated manifest is fetched without target filtering or lock changes."""
    repository = create_repository(tmp_path / "repo")
    cargo_home = tmp_path / "run-cargo-home"
    cargo_home.mkdir()
    manifest = repository / "rust/Cargo.toml"
    lockfile = manifest.parent / "Cargo.lock"
    original_lock = lockfile.read_bytes()
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def run_cargo(
        arguments: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
    ) -> SimpleNamespace:
        """Record the locked fetch and return a successful Cargo result."""
        calls.append((arguments, cwd, environment.copy()))
        return SimpleNamespace(exit_code=0, stdout="", stderr="")

    monkeypatch.setattr(cargo_state, "_run_cargo", run_cargo)
    rustup_home = tmp_path / "rustup"
    elapsed = cargo_state.fetch_workspace_dependencies(
        manifest,
        cargo_home,
        context=cargo_state.PreparationContext(
            caller_environment={
                "CARGO_BUILD_JOBS": "5",
                "CARGO_NET_OFFLINE": "true",
                "RUSTUP_HOME": str(rustup_home),
            },
        ),
    )

    assert calls[0][0] == (
        "fetch",
        "--locked",
        "--manifest-path",
        str(manifest),
    ), "workspace preparation must use the locked manifest without target filters"
    assert calls[0][1] == manifest.parent, (
        "Cargo must resolve from the copied workspace"
    )
    assert calls[0][2]["CARGO_HOME"] == str(cargo_home), (
        "dependency preparation must use the run-local writable Cargo home"
    )
    assert calls[0][2]["CARGO_BUILD_JOBS"] == "5", (
        "preparation must preserve caller-selected Cargo parallelism"
    )
    assert calls[0][2]["RUSTUP_HOME"] == str(rustup_home), (
        "preparation must pin the caller's rustup home"
    )
    assert calls[0][2]["CARGO_NET_OFFLINE"] == "false", (
        "isolated workspace dependencies must be fetched before timed offline builds"
    )
    assert lockfile.read_bytes() == original_lock, (
        "locked preparation must preserve the copied lockfile"
    )
    assert elapsed >= 0, (
        "preparation duration must use a non-negative monotonic interval"
    )


def test_run_homes_are_independent_copies_and_preserve_job_settings(
    tmp_path: Path,
) -> None:
    """Independent runs get distinct writable homes and keep Cargo job choices."""
    snapshot = tmp_path / "snapshot"
    source = snapshot / "registry/cache/crate"
    source.parent.mkdir(parents=True)
    source.write_text("archive", encoding="utf-8")
    first = cargo_state.create_run_cargo_home(snapshot, tmp_path / "run-one")
    second = cargo_state.create_run_cargo_home(snapshot, tmp_path / "run-two")
    caller_environment = {
        "CARGO_BUILD_JOBS": "9",
        "RUSTUP_HOME": str(tmp_path / "rustup-home"),
    }

    first_copy = first / "registry/cache/crate"
    second_copy = second / "registry/cache/crate"
    first_environment = cargo_state.cargo_environment(first, caller_environment)

    assert first != second, "concurrent runs must use different Cargo homes"
    assert first_copy.read_text(encoding="utf-8") == "archive", (
        "each home must contain the prepared cache seed"
    )
    assert second_copy.read_text(encoding="utf-8") == "archive", (
        "each home must contain the prepared cache seed"
    )
    assert first_copy.stat().st_ino != source.stat().st_ino, (
        "run homes must not hard-link to the read-only snapshot"
    )
    assert first_copy.stat().st_ino != second_copy.stat().st_ino, (
        "run homes must not hard-link to one another"
    )
    assert not first_copy.is_symlink(), "the first run home must not redirect writes"
    assert not second_copy.is_symlink(), "the second run home must not redirect writes"
    assert first_copy.stat().st_mode & stat.S_IWUSR, (
        "the first run-local home must be writable by Cargo"
    )
    assert second_copy.stat().st_mode & stat.S_IWUSR, (
        "the second run-local home must be writable by Cargo"
    )
    assert first_environment["CARGO_HOME"] == str(first), "Cargo home must be run-local"
    assert first_environment["RUSTUP_HOME"] == caller_environment["RUSTUP_HOME"], (
        "the caller's rustup home must stay pinned"
    )
    assert first_environment["CARGO_BUILD_JOBS"] == "9", (
        "run setup must not override caller-selected Cargo parallelism"
    )


def test_a_failed_copy_removes_the_partial_read_only_run_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A partial copy must not leave read-only files behind a full disk."""
    snapshot = tmp_path / "snapshot"
    seeded = snapshot / "registry/cache/crate"
    seeded.parent.mkdir(parents=True)
    seeded.write_text("archive", encoding="utf-8")
    for path in (snapshot, *snapshot.rglob("*")):
        path.chmod(path.stat().st_mode & ~0o222)

    def fail_after_copying(source: Path, destination: Path) -> Path:
        """Mimic a partial copy that inherited the snapshot's read-only modes."""
        destination.mkdir(parents=True)
        mirrored = destination / "registry/cache/crate"
        mirrored.parent.mkdir(parents=True)
        mirrored.write_bytes((source / "registry/cache/crate").read_bytes())
        for path in (mirrored.parent, mirrored):
            path.chmod(path.stat().st_mode & ~0o222)
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(cargo_run_home.shutil, "copytree", fail_after_copying)
    run_root = tmp_path / "run"

    with pytest.raises(OSError, match="No space left"):
        cargo_state.create_run_cargo_home(snapshot, run_root)

    assert not (run_root / "cargo-home").exists(), (
        "a half-populated read-only home must not survive a failed copy"
    )
