"""Check immutable Cargo snapshots and independent writable run homes."""

from __future__ import annotations

import dataclasses as dc
import errno
import stat
import typing as typ
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import cargo_state
from scripts.cargo_cache_tree import publish_snapshot
from scripts.tests.cargo_state_support import create_repository

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SEEDED_CARGO_FILES = {
    "registry/cache/registry.crate": "registry bytes",
    "registry/src/registry-1.0.0/Cargo.toml": "stale extracted source",
    "git/db/source.git/config": "git bytes",
    "git/checkouts/stale-source/checkout/Cargo.toml": "stale git checkout",
    "config.toml": "[net]\noffline = false\n",
    "credentials.toml": "token = 'private-token'\n",
}


@dc.dataclass(frozen=True, slots=True)
class _SnapshotInputs:
    """Hold inputs used to verify one staged Cargo snapshot preparation."""

    repository: Path
    source_home: Path
    snapshot_root: Path
    original_lock: bytes
    caller_environment: dict[str, str]


def _snapshot_inputs(tmp_path: Path) -> _SnapshotInputs:
    """Create the workspace and seeded caller cache for a snapshot test."""
    repository = create_repository(tmp_path / "repo")
    source_home = tmp_path / "caller-cargo"
    for relative, contents in _SEEDED_CARGO_FILES.items():
        path = source_home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
    lockfile = repository / "rust/Cargo.lock"
    return _SnapshotInputs(
        repository=repository,
        source_home=source_home,
        snapshot_root=tmp_path / "snapshots",
        original_lock=lockfile.read_bytes(),
        caller_environment={
            "CARGO_HOME": str(source_home),
            "RUSTUP_HOME": str(tmp_path / "rustup-home"),
            "CARGO_BUILD_JOBS": "7",
            "CARGO_NET_OFFLINE": "true",
        },
    )


def _record_snapshot_cargo(
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]],
    fetch_credentials: list[bool],
) -> cabc.Callable[[tuple[str, ...], Path, dict[str, str]], SimpleNamespace]:
    """Return a Cargo stub that records commands and adds a fetch marker."""

    def run_cargo(
        arguments: tuple[str, ...],
        cwd: Path,
        environment: dict[str, str],
    ) -> SimpleNamespace:
        """Record Cargo and add a marker after dependency preparation."""
        calls.append((arguments, cwd, environment.copy()))
        if arguments[0] == "fetch":
            fetch_credentials.append(
                (Path(environment["CARGO_HOME"]) / "credentials.toml").is_file()
            )
            marker = Path(environment["CARGO_HOME"]) / "registry/fetched.marker"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("prepared", encoding="utf-8")
        return SimpleNamespace(exit_code=0, stdout="cargo 1.85.0\n", stderr="")

    return run_cargo


def _assert_fetch_setup(
    inputs: _SnapshotInputs,
    prepared: cargo_state.PreparedCargoSnapshot,
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]],
    fetch_credentials: list[bool],
) -> None:
    """Verify Cargo fetch used the staged home and preserved caller policy."""
    assert [call[0] for call in calls] == [
        ("--version",),
        (
            "fetch",
            "--locked",
            "--manifest-path",
            str(inputs.repository / "rust/Cargo.toml"),
        ),
    ], "preparation must fetch the locked workspace without restricting targets"
    assert [call[1] for call in calls] == [
        inputs.repository / "rust",
        inputs.repository / "rust",
    ], "Cargo version and fetch commands must use the pinned Rust workspace"
    fetch_environment = calls[1][2]
    staging_home = Path(fetch_environment["CARGO_HOME"])
    assert staging_home != inputs.source_home, (
        "Cargo fetch must populate a staged home, not the caller cache"
    )
    assert staging_home.name.startswith(f".{prepared.key}.staging-"), (
        "Cargo fetch must populate an unpublished snapshot directory"
    )
    assert not staging_home.exists(), "the staging name must be absent after publish"
    assert (
        fetch_environment["RUSTUP_HOME"] == inputs.caller_environment["RUSTUP_HOME"]
    ), "preparation must pin the caller's rustup home"
    assert fetch_environment["CARGO_BUILD_JOBS"] == "7", (
        "preparation must preserve caller-selected Cargo parallelism"
    )
    assert fetch_environment["CARGO_NET_OFFLINE"] == "false", (
        "dependency preparation must fetch before the offline phase"
    )
    assert fetch_credentials == [True], (
        "Cargo fetch must be able to authenticate with copied caller credentials"
    )


def _assert_snapshot_contents(
    inputs: _SnapshotInputs,
    prepared: cargo_state.PreparedCargoSnapshot,
) -> None:
    """Verify seeded cache copies, fetched state, credentials, and lockfile."""
    for relative, contents in _SEEDED_CARGO_FILES.items():
        if relative in {"credentials", "credentials.toml"}:
            assert not (prepared.path / relative).exists(), (
                "published Cargo snapshots must not retain authentication secrets"
            )
            continue
        if relative.startswith(("registry/src/", "git/checkouts/")):
            continue
        copied = prepared.path / relative
        assert copied.read_text(encoding="utf-8") == contents, (
            f"snapshot must seed caller Cargo state at {relative}"
        )
        assert copied.stat().st_ino != (inputs.source_home / relative).stat().st_ino, (
            f"snapshot entries must be copies, not hard links: {relative}"
        )
        assert not copied.is_symlink(), (
            f"snapshot entries must not be symlinks: {relative}"
        )
        assert not copied.stat().st_mode & stat.S_IWUSR, (
            f"published snapshot entries must be read-only: {relative}"
        )
    assert not (prepared.path / "registry/src/registry-1.0.0").exists(), (
        "stale extracted registry versions must be recreated from this lockfile"
    )
    assert not (prepared.path / "git/checkouts/stale-source").exists(), (
        "stale git checkouts must not inflate the prepared snapshot"
    )
    assert (prepared.path / "registry/fetched.marker").is_file(), (
        "the successful fetch must be present in the published snapshot"
    )
    assert (
        inputs.repository / "rust/Cargo.lock"
    ).read_bytes() == inputs.original_lock, (
        "--locked preparation must preserve Cargo.lock"
    )
    assert not list(inputs.snapshot_root.glob(".*.staging-*")), (
        "successful publication must leave no staging directory behind"
    )
    assert (inputs.source_home / "credentials.toml").read_text(encoding="utf-8") == (
        _SEEDED_CARGO_FILES["credentials.toml"]
    ), "seeding must not modify caller credentials"


def test_preparation_fetches_locked_dependencies_into_a_staged_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fetch uses the copied home, preserves caller jobs, and publishes atomically."""
    inputs = _snapshot_inputs(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []
    fetch_credentials: list[bool] = []
    monkeypatch.setattr(
        cargo_state,
        "_run_cargo",
        _record_snapshot_cargo(calls, fetch_credentials),
    )

    prepared = cargo_state.prepare_cargo_snapshot(
        inputs.repository,
        inputs.snapshot_root,
        caller_environment=inputs.caller_environment,
    )
    _assert_fetch_setup(inputs, prepared, calls, fetch_credentials)
    _assert_snapshot_contents(inputs, prepared)


@pytest.mark.parametrize("race_error", [errno.EEXIST, errno.ENOTEMPTY])
def test_snapshot_publish_accepts_a_competing_complete_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_error: int,
) -> None:
    """A concurrent publisher is accepted only when its directory exists."""
    staging = tmp_path / "staging"
    staging.mkdir()
    destination = tmp_path / "snapshot"
    destination.mkdir()
    (destination / "complete.marker").write_text("ready", encoding="utf-8")

    def lose_rename_race(_path: Path, _destination: Path) -> Path:
        """Model another process publishing the same snapshot first."""
        raise OSError(race_error, "destination already published")

    monkeypatch.setattr(Path, "rename", lose_rename_race)

    assert not publish_snapshot(staging, destination), (
        "a non-empty published destination means this process lost the race"
    )


def test_losing_snapshot_publication_removes_read_only_staging_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only staging home is made writable before its cleanup."""
    inputs = _snapshot_inputs(tmp_path)

    def run_cargo(
        arguments: tuple[str, ...],
        _cwd: Path,
        _environment: dict[str, str],
    ) -> SimpleNamespace:
        """Complete locked preparation without downloading external packages."""
        return SimpleNamespace(exit_code=0, stdout="cargo 1.85.0\n", stderr="")

    def publish_winner(staging: Path, snapshot: Path) -> bool:
        """Expose a complete winner and report that this staging tree lost."""
        snapshot.mkdir()
        (snapshot / "complete.marker").write_text("ready", encoding="utf-8")
        return False

    monkeypatch.setattr(cargo_state, "_run_cargo", run_cargo)
    monkeypatch.setattr(cargo_state, "_publish_snapshot", publish_winner)

    prepared = cargo_state.prepare_cargo_snapshot(
        inputs.repository,
        inputs.snapshot_root,
        inputs.source_home,
        caller_environment=inputs.caller_environment,
    )

    assert prepared.reused, "the losing publisher must return the winner's path"
    assert (prepared.path / "complete.marker").read_text(encoding="utf-8") == "ready", (
        "a concurrent complete snapshot must remain intact"
    )
    assert not list(inputs.snapshot_root.glob(".*.staging-*")), (
        "read-only staging cleanup must finish after a publication race"
    )


def test_existing_snapshot_skips_fetch_but_reads_cargo_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prepared snapshot is reused without another dependency fetch."""
    repository = create_repository(tmp_path / "repo")
    calls: list[tuple[str, ...]] = []

    def run_cargo(
        arguments: tuple[str, ...],
        _cwd: Path,
        _environment: dict[str, str],
    ) -> SimpleNamespace:
        """Return a Cargo version and record all preparation commands."""
        calls.append(arguments)
        return SimpleNamespace(exit_code=0, stdout="cargo 1.85.0\n", stderr="")

    monkeypatch.setattr(cargo_state, "_run_cargo", run_cargo)
    snapshot_root = tmp_path / "snapshots"
    first = cargo_state.prepare_cargo_snapshot(
        repository, snapshot_root, tmp_path / "missing-cargo-home"
    )
    second = cargo_state.prepare_cargo_snapshot(
        repository, snapshot_root, tmp_path / "missing-cargo-home"
    )

    assert len(calls) == 3, (
        "snapshot reuse must perform one fetch and two version reads"
    )
    assert calls[0] == ("--version",), "preparation must read Cargo's version first"
    assert calls[1][0] == "fetch", "the initial snapshot must fetch dependencies"
    assert calls[1][1:3] == ("--locked", "--manifest-path"), (
        "the snapshot fetch must preserve the lockfile"
    )
    assert calls[1][3] == str(repository / "rust/Cargo.toml"), (
        "the snapshot fetch must target the workspace manifest"
    )
    assert calls[2] == ("--version",), "reuse must still check the Cargo version"
    assert second.path == first.path, "reuse must return the existing snapshot"
    assert second.reused, "the result must identify snapshot reuse"


def test_missing_caller_home_is_not_created_during_snapshot_seeding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Snapshot preparation reads a caller cache without mutating its root."""
    repository = create_repository(tmp_path / "repo")
    missing_home = tmp_path / "not-created-cargo-home"

    def run_cargo(
        _arguments: tuple[str, ...],
        _cwd: Path,
        _environment: dict[str, str],
    ) -> SimpleNamespace:
        """Return the Cargo version without creating the caller cache root."""
        return SimpleNamespace(exit_code=0, stdout="cargo 1.85.0\n", stderr="")

    monkeypatch.setattr(cargo_state, "_run_cargo", run_cargo)
    cargo_state.prepare_cargo_snapshot(
        repository,
        tmp_path / "snapshots",
        missing_home,
    )

    assert not missing_home.exists(), (
        "seeding must not create or mutate caller Cargo home"
    )


def test_fetch_failure_hides_diagnostics_and_removes_staging_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fetch failure is bounded and does not leak Cargo credential output."""
    repository = create_repository(tmp_path / "repo")
    snapshot_root = tmp_path / "snapshots"
    diagnostic_marker = "do-not-leak-marker"

    def run_cargo(
        arguments: tuple[str, ...],
        _cwd: Path,
        _environment: dict[str, str],
    ) -> SimpleNamespace:
        """Return credential-like output only for the failing fetch."""
        if arguments[0] == "fetch":
            return SimpleNamespace(
                exit_code=17,
                stdout=diagnostic_marker,
                stderr=diagnostic_marker,
            )
        return SimpleNamespace(exit_code=0, stdout="cargo 1.85.0\n", stderr="")

    monkeypatch.setattr(cargo_state, "_run_cargo", run_cargo)

    with pytest.raises(cargo_state.CargoStatePreparationError) as error:
        cargo_state.prepare_cargo_snapshot(
            repository,
            snapshot_root,
            tmp_path / "empty-caller-cargo-home",
        )

    assert diagnostic_marker not in str(error.value), (
        "Cargo diagnostics may contain credential data"
    )
    assert not list(snapshot_root.glob(".*.staging-*")), (
        "failed preparation must remove its temporary Cargo home"
    )
