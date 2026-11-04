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
    _prune_registry_archives,
    remove_cargo_credentials,
    remove_cargo_settings,
    selected_registry_archives,
)
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
from scripts.cargo_run_home import create_run_cargo_home
from scripts.cargo_state_command import _run_cargo
from scripts.cargo_state_errors import CargoStatePreparationError
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
    """Describe one prepared, immutable cache snapshot and its preparation.

    ``source_cargo_home`` records the caller home whose current configuration
    each run home receives. The snapshot itself holds cache data only, so a
    reused snapshot cannot pin stale Cargo settings.
    """

    path: Path
    key: str
    cargo_version: str
    source_cargo_home: Path
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
        _reject_tree_symlinks_impl(snapshot_path, CargoStatePreparationError)
        _make_tree_read_only(snapshot_path)
    return PreparedCargoSnapshot(
        path=snapshot_path,
        key=request.key,
        cargo_version=request.cargo_version,
        source_cargo_home=request.source_cargo_home,
        preparation_seconds=time.monotonic() - request.started,
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
    manifest = request.repository_root / "rust/Cargo.toml"
    lockfile = manifest.parent / "Cargo.lock"
    try:
        _seed_cargo_home_impl(
            request.source_cargo_home,
            temporary_path,
            CargoStatePreparationError,
            selected_archives=selected_registry_archives(lockfile),
        )
        _fetch_locked(
            manifest,
            _cargo_environment(
                temporary_path,
                request.rustup_home,
                request.caller_environment,
            ),
        )

        # The fetch needed the caller's configuration and credentials, but a
        # published snapshot must hold only cache data: settings are applied
        # afresh to each run home, so they cannot go stale behind a shared key.
        remove_cargo_credentials(temporary_path)
        remove_cargo_settings(temporary_path)
        _reject_tree_symlinks_impl(temporary_path, CargoStatePreparationError)
        _prune_registry_archives(temporary_path, lockfile)
        _reject_tree_symlinks_impl(temporary_path, CargoStatePreparationError)
        _make_tree_read_only(temporary_path)
        reused = not _publish_snapshot(temporary_path, request.snapshot_path)
        return PreparedCargoSnapshot(
            path=request.snapshot_path,
            key=request.key,
            cargo_version=request.cargo_version,
            source_cargo_home=request.source_cargo_home,
            preparation_seconds=time.monotonic() - request.started,
            reused=reused,
        )
    finally:
        if temporary_path.exists():
            _make_tree_writable(temporary_path)
            shutil.rmtree(temporary_path)


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
    environment = dict(os.environ if caller_environment is None else caller_environment)
    started = time.monotonic()
    _fetch_locked(manifest, cargo_environment(cargo_home, environment))
    return time.monotonic() - started


def _fetch_locked(manifest: Path, environment: cabc.Mapping[str, str]) -> None:
    """Fetch one workspace's locked dependencies and reject rewritten inputs.

    Network access is enabled only for this preparation call so Cargo can
    populate the cache, while the digest comparison afterwards proves
    ``--locked`` held.

    Raises
    ------
    CargoStatePreparationError
        If Cargo fails or the fetch changes the manifest's sibling lockfile.
    """
    lockfile = manifest.parent / "Cargo.lock"
    lockfile_digest = _sha256(lockfile)
    fetch_environment = dict(environment)
    fetch_environment["CARGO_NET_OFFLINE"] = "false"
    result = _run_cargo(
        ("fetch", "--locked", "--manifest-path", str(manifest)),
        manifest.parent,
        fetch_environment,
    )
    if result.exit_code != 0:
        msg = f"cargo fetch --locked failed with exit code {result.exit_code}"
        raise CargoStatePreparationError(msg)
    if _sha256(lockfile) != lockfile_digest:
        msg = f"cargo fetch changed {lockfile.name} during locked preparation"
        raise CargoStatePreparationError(msg)
