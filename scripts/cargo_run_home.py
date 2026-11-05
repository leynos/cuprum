"""Create writable per-run Cargo homes from immutable cache snapshots."""

from __future__ import annotations

import contextlib
import shutil
import typing as typ

from scripts.cargo_cache_tree import (
    _prepare_run_home,
    _require_real_directory,
    copy_cargo_settings,
    make_tree_writable,
    reject_tree_symlinks,
    remove_cargo_credentials,
    remove_cargo_settings,
)
from scripts.cargo_state_errors import CargoStatePreparationError

if typ.TYPE_CHECKING:
    from pathlib import Path


def create_run_cargo_home(
    snapshot: Path,
    run_root: Path,
    *,
    source_cargo_home: Path | None = None,
) -> Path:
    """Copy a snapshot into a new writable home owned by one top-level run.

    Published snapshots hold cache data only. When ``source_cargo_home`` is
    supplied, its current ``config`` or ``config.toml`` is copied into the new
    run home, so a reused snapshot cannot pin stale settings; credentials are
    never copied. Any settings carried by an older snapshot are removed first.

    Parameters
    ----------
    snapshot : Path
        Read-only, prepared cache snapshot to copy.
    run_root : Path
        Existing or new directory owned by this top-level run.
    source_cargo_home : Path, optional
        Caller Cargo home supplying the current configuration. When omitted,
        the run home receives no configuration files.

    Returns
    -------
    Path
        Newly created writable ``cargo-home`` directory beneath ``run_root``.

    Raises
    ------
    CargoStatePreparationError
        If the snapshot is not a real directory, contains a symlink, the run
        root is a symlink, the run-local ``cargo-home`` already exists, or a
        source configuration file is a symlink.
    OSError
        If the run home cannot be created or copied.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem errors propagate from directory creation and copying.
    _require_real_directory(snapshot, CargoStatePreparationError, "Cargo snapshot")
    reject_tree_symlinks(snapshot, CargoStatePreparationError)
    run_home = _prepare_run_home(run_root, CargoStatePreparationError)
    try:
        shutil.copytree(snapshot, run_home)
        # Snapshots are read-only, and an older one may still carry settings or
        # credentials from before published snapshots excluded them. Restore
        # write access before dropping them, then apply the caller's current
        # settings.
        make_tree_writable(run_home)
    except BaseException:
        # copytree mirrors the snapshot's read-only modes even when it fails
        # part-way, so a half-populated home cannot be removed by the callers'
        # best-effort cleanup. Restore write access first, then drop it, so a
        # full disk is not left holding the space that caused the failure.
        with contextlib.suppress(OSError):
            make_tree_writable(run_home)
        shutil.rmtree(run_home, ignore_errors=True)
        raise
    remove_cargo_settings(run_home)
    remove_cargo_credentials(run_home)
    if source_cargo_home is not None:
        copy_cargo_settings(
            source_cargo_home,
            run_home,
            CargoStatePreparationError,
        )
    return run_home
