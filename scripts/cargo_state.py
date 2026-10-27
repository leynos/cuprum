"""Prepare immutable Cargo cache snapshots and private run-local homes."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shutil
import stat
import tempfile
import time
import typing as typ
from pathlib import Path

from scripts.cargo_cache_tree import (
    make_tree_read_only as _make_tree_read_only,
)
from scripts.cargo_cache_tree import (
    make_tree_writable as _make_tree_writable,
)
from scripts.cargo_cache_tree import (
    publish_snapshot as _publish_snapshot,
)
from scripts.cargo_cache_tree import (
    reject_tree_symlinks as _reject_tree_symlinks_impl,
)
from scripts.cargo_cache_tree import (
    seed_cargo_home as _seed_cargo_home_impl,
)
from scripts.cargo_environment import (
    _caller_cargo_home,
    _cargo_environment,
    _pinned_rustup_home,
    cargo_environment,
)
from scripts.cargo_state_command import _run_cargo
from scripts.cargo_state_metadata import (
    _sha256,
    cargo_snapshot_key,
    default_snapshot_root,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "CargoStatePreparationError",
    "PreparedCargoSnapshot",
    "cargo_environment",
    "cargo_snapshot_key",
    "create_run_cargo_home",
    "default_snapshot_root",
    "fetch_workspace_dependencies",
    "prepare_cargo_snapshot",
    "record_phase",
)


@dc.dataclass(frozen=True, slots=True)
class PreparedCargoSnapshot:
    """Describe one prepared, immutable cache snapshot and its preparation."""

    path: Path
    key: str
    cargo_version: str
    preparation_seconds: float
    reused: bool


@dc.dataclass(frozen=True, slots=True)
class _SnapshotPreparation:
    """Carry the immutable inputs needed to publish one cache snapshot."""

    repository_root: Path
    snapshot_root: Path
    source_cargo_home: Path
    rustup_home: str
    caller_environment: cabc.Mapping[str, str]
    cargo_version: str
    key: str
    started: float

    @property
    def snapshot_path(self) -> Path:
        """Cache path derived from this snapshot's complete key."""
        return self.snapshot_root / self.key


class CargoStatePreparationError(RuntimeError):
    """Report a Cargo preparation failure without exposing cache credentials."""


def record_phase(timings_path: Path, phase: str, seconds: float) -> None:
    """Append a monotonic phase duration without recording environment data.

    Parameters
    ----------
    timings_path : Path
        Run-local JSON-lines file receiving the phase record.
    phase : str
        Stable name for the measured phase.
    seconds : float
        Elapsed monotonic duration in seconds.

    Raises
    ------
    OSError
        If the timing file cannot be opened or written.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from Path.open and write.
    with timings_path.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"phase": phase, "seconds": seconds}) + "\n")


def prepare_cargo_snapshot(
    repository_root: Path,
    snapshot_root: Path,
    source_cargo_home: Path | None = None,
    *,
    caller_environment: cabc.Mapping[str, str] | None = None,
) -> PreparedCargoSnapshot:
    """Prepare and atomically publish a locked, all-target Cargo cache snapshot.

    The snapshot is copied into each run before builds begin. Its contents are
    never passed as a writable home to concurrent Cargo processes.

    Parameters
    ----------
    repository_root : Path
        Repository whose Rust workspace lockfile and toolchain define the key.
    snapshot_root : Path
        Directory that stores immutable snapshots under their content keys.
    source_cargo_home : Path, optional
        Cargo home used to seed the temporary cache before locked fetch.
        Defaults to the caller's configured or default Cargo home.
    caller_environment : Mapping of str to str, optional
        Environment used to resolve caller cache paths and Cargo settings.

    Returns
    -------
    PreparedCargoSnapshot
        Published snapshot metadata and the time spent preparing dependencies.

    Raises
    ------
    CargoStatePreparationError
        If Cargo cannot report its version, locked fetching fails, or snapshot
        publication does not preserve the locked workspace inputs.
    OSError
        If the repository files or snapshot cache cannot be read or written.

    """  # ruff: ignore[docstring-extraneous-exception] - errors propagate from file, subprocess, and copy helpers.
    started = time.monotonic()
    environment = dict(os.environ if caller_environment is None else caller_environment)
    source_home = source_cargo_home or _caller_cargo_home(environment)
    rustup_home = _pinned_rustup_home(environment)
    cargo_version = _cargo_version(
        repository_root=repository_root,
        cargo_home=source_home,
        rustup_home=rustup_home,
        caller_environment=environment,
    )
    request = _SnapshotPreparation(
        repository_root=repository_root,
        snapshot_root=snapshot_root,
        source_cargo_home=source_home,
        rustup_home=rustup_home,
        caller_environment=environment,
        cargo_version=cargo_version,
        key=cargo_snapshot_key(repository_root, cargo_version),
        started=started,
    )
    existing = _existing_snapshot(request)
    return existing if existing is not None else _create_snapshot(request)


def _cargo_version(
    *,
    repository_root: Path,
    cargo_home: Path,
    rustup_home: str,
    caller_environment: cabc.Mapping[str, str],
) -> str:
    """Read Cargo's version using the caller's pinned Rustup installation.

    Returns
    -------
    str
        Cargo's version string.

    Raises
    ------
    CargoStatePreparationError
        If Cargo exits unsuccessfully or returns no version.
    """
    environment = _cargo_environment(cargo_home, rustup_home, caller_environment)
    result = _run_cargo(("--version",), repository_root / "rust", environment)
    if result.exit_code != 0:
        msg = f"cargo --version failed with exit code {result.exit_code}"
        raise CargoStatePreparationError(msg)
    version = (result.stdout or "").strip()
    if not version:
        msg = "cargo --version returned no version"
        raise CargoStatePreparationError(msg)
    return version


def _existing_snapshot(
    request: _SnapshotPreparation,
) -> PreparedCargoSnapshot | None:
    """Validate and reuse a complete immutable snapshot when present."""
    snapshot_path = request.snapshot_path
    if snapshot_path.is_symlink():
        msg = f"Cargo snapshot path must not be a symlink: {snapshot_path}"
        raise CargoStatePreparationError(msg)
    if not snapshot_path.is_dir():
        return None

    snapshot_mode = stat.S_IMODE(snapshot_path.stat().st_mode)
    if snapshot_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
        _reject_tree_symlinks(snapshot_path)
        _make_tree_read_only(snapshot_path)
    return PreparedCargoSnapshot(
        snapshot_path,
        request.key,
        request.cargo_version,
        time.monotonic() - request.started,
        reused=True,
    )


def _create_snapshot(request: _SnapshotPreparation) -> PreparedCargoSnapshot:
    """Fetch locked dependencies into a staging home and publish it atomically."""
    request.snapshot_root.mkdir(parents=True, exist_ok=True)
    temporary_path = Path(
        tempfile.mkdtemp(
            prefix=f".{request.key}.staging-",
            dir=request.snapshot_root,
        )
    )
    try:
        _seed_cargo_home(request.source_cargo_home, temporary_path)
        lockfile = request.repository_root / "rust/Cargo.lock"
        lockfile_digest = _sha256(lockfile)
        fetch_environment = _cargo_environment(
            temporary_path,
            request.rustup_home,
            request.caller_environment,
        )
        fetch_environment["CARGO_NET_OFFLINE"] = "false"
        fetch_arguments = (
            "fetch",
            "--locked",
            "--manifest-path",
            str(request.repository_root / "rust/Cargo.toml"),
        )
        result = _run_cargo(
            fetch_arguments,
            request.repository_root / "rust",
            fetch_environment,
        )
        if result.exit_code != 0:
            msg = f"cargo fetch --locked failed with exit code {result.exit_code}"
            raise CargoStatePreparationError(msg)
        if _sha256(lockfile) != lockfile_digest:
            msg = "cargo fetch changed rust/Cargo.lock despite --locked"
            raise CargoStatePreparationError(msg)

        _remove_credentials(temporary_path)
        _reject_tree_symlinks(temporary_path)
        _make_tree_read_only(temporary_path)
        reused = not _publish_snapshot(temporary_path, request.snapshot_path)
        return PreparedCargoSnapshot(
            request.snapshot_path,
            request.key,
            request.cargo_version,
            time.monotonic() - request.started,
            reused,
        )
    finally:
        if temporary_path.exists():
            _make_tree_writable(temporary_path)
            shutil.rmtree(temporary_path)


def _remove_credentials(cargo_home: Path) -> None:
    """Keep fetch credentials out of persistent and run-local snapshots."""
    for name in ("credentials", "credentials.toml"):
        (cargo_home / name).unlink(missing_ok=True)


def fetch_workspace_dependencies(
    workspace_manifest: Path,
    cargo_home: Path,
    *,
    caller_environment: cabc.Mapping[str, str] | None = None,
) -> float:
    """Fetch a copied workspace's locked dependencies before its build budget.

    The manifest's sibling lockfile is checked after Cargo exits so a
    preparation route cannot silently rewrite the workspace inputs.

    Parameters
    ----------
    workspace_manifest : Path
        Copied workspace's ``Cargo.toml`` path.
    cargo_home : Path
        Writable Cargo home owned by the current run.
    caller_environment : Mapping of str to str, optional
        Environment copied into the fetch process before pinning Cargo homes.

    Returns
    -------
    float
        Monotonic preparation duration in seconds.

    Raises
    ------
    CargoStatePreparationError
        If no lockfile exists, Cargo fails, or the fetch changes the lockfile.
    OSError
        If the manifest or sibling lockfile cannot be read.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from path resolution and lockfile reads.
    manifest = workspace_manifest.resolve()
    lockfile = manifest.parent / "Cargo.lock"
    if not lockfile.is_file():
        msg = f"locked Cargo preparation requires {lockfile}"
        raise CargoStatePreparationError(msg)
    lockfile_digest = _sha256(lockfile)
    environment = dict(os.environ if caller_environment is None else caller_environment)
    fetch_environment = cargo_environment(cargo_home, environment)
    fetch_environment["CARGO_NET_OFFLINE"] = "false"
    started = time.monotonic()
    result = _run_cargo(
        ("fetch", "--locked", "--manifest-path", str(manifest)),
        manifest.parent,
        fetch_environment,
    )
    elapsed = time.monotonic() - started
    if result.exit_code != 0:
        msg = f"cargo fetch --locked failed with exit code {result.exit_code}"
        raise CargoStatePreparationError(msg)
    if _sha256(lockfile) != lockfile_digest:
        msg = f"cargo fetch changed {lockfile.name} despite --locked"
        raise CargoStatePreparationError(msg)
    return elapsed


def create_run_cargo_home(snapshot: Path, run_root: Path) -> Path:
    """Copy a snapshot into a new writable home owned by one top-level run.

    Parameters
    ----------
    snapshot : Path
        Read-only, prepared cache snapshot to copy.
    run_root : Path
        Existing or new directory owned by this top-level run.

    Returns
    -------
    Path
        Newly created writable ``cargo-home`` directory beneath ``run_root``.

    Raises
    ------
    CargoStatePreparationError
        If the snapshot is not a real directory, contains a symlink, the run
        root is a symlink, or the run-local ``cargo-home`` already exists.
    OSError
        If the run home cannot be created or copied.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from directory creation and copying.
    if snapshot.is_symlink() or not snapshot.is_dir():
        msg = f"Cargo snapshot must be a real directory: {snapshot}"
        raise CargoStatePreparationError(msg)
    _reject_tree_symlinks(snapshot)
    run_root.mkdir(parents=True, exist_ok=True)
    if run_root.is_symlink():
        msg = f"run root must not be a symlink: {run_root}"
        raise CargoStatePreparationError(msg)
    run_home = run_root / "cargo-home"
    if run_home.exists() or run_home.is_symlink():
        msg = f"run-local Cargo home already exists: {run_home}"
        raise CargoStatePreparationError(msg)
    shutil.copytree(snapshot, run_home)
    _make_tree_writable(run_home)
    return run_home


def _seed_cargo_home(source: Path, destination: Path) -> None:
    """Seed the staging home while keeping cache errors domain-specific."""
    _seed_cargo_home_impl(source, destination, CargoStatePreparationError)


def _reject_tree_symlinks(root: Path) -> None:
    """Reject symlinked cache entries with a preparation-specific failure."""
    _reject_tree_symlinks_impl(root, CargoStatePreparationError)
