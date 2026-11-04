"""Prepare Cargo state for copied boundary workspaces before compilation."""

from __future__ import annotations

import contextlib
import shutil
import tempfile
import time
import typing as typ
from pathlib import Path

from scripts.cargo_environment import cargo_environment
from scripts.cargo_state import (
    create_run_cargo_home,
    default_snapshot_root,
    fetch_workspace_dependencies,
    prepare_cargo_snapshot,
    record_phase,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def offline_cargo_environment(
    workspace: Path,
    target_directory: Path,
    caller_environment: cabc.Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return an offline Cargo environment rooted in its prepared run home.

    Parameters
    ----------
    workspace : Path
        Copied workspace whose parent contains the run-local Cargo home.
    target_directory : Path
        Configuration-compatible Cargo target directory for this invocation.
    caller_environment : Mapping of str to str, optional
        Environment to preserve before setting the Cargo-specific paths.
        Defaults to the current process environment.

    Returns
    -------
    dict of str to str
        Environment selecting the prepared run home, target directory and
        offline network mode while preserving caller-selected Cargo jobs.

    Raises
    ------
    FileNotFoundError
        If the run-local Cargo home is missing or is a symlink.
    """
    cargo_home = workspace.parent / "cargo-home"
    if not cargo_home.is_dir() or cargo_home.is_symlink():
        msg = f"prepared run-local Cargo home is unavailable: {cargo_home}"
        raise FileNotFoundError(msg)
    environment = cargo_environment(cargo_home, caller_environment)
    environment["CARGO_TARGET_DIR"] = str(target_directory)
    environment["CARGO_NET_OFFLINE"] = "true"
    return environment


def prepare_run_cargo_home(
    repository_root: Path,
    run_root: Path,
    workspace: Path,
) -> float:
    """Seed a private Cargo home and fetch copied-workspace dependencies.

    Errors from snapshot preparation, Cargo-home creation, or dependency
    fetching propagate to the caller.

    Parameters
    ----------
    repository_root : Path
        Repository root that supplies the prepared Cargo snapshot.
    run_root : Path
        Run directory that receives the private ``cargo-home``.
    workspace : Path
        Copied workspace whose dependencies are prepared before compilation.

    Returns
    -------
    float
        Elapsed dependency-preparation time in seconds, measured monotonically.

    Raises
    ------
    CargoStatePreparationError
        If snapshot preparation, cache copying, or locked dependency fetching
        fails.
    OSError
        If the run home or its files cannot be created or copied.
    """  # ruff: ignore[docstring-extraneous-exception] - failures propagate from the preparation helpers.
    started = time.monotonic()
    snapshot = prepare_cargo_snapshot(repository_root, default_snapshot_root())
    cargo_home = create_run_cargo_home(
        snapshot.path,
        run_root,
        source_cargo_home=snapshot.source_cargo_home,
    )
    fetch_workspace_dependencies(
        workspace / "Cargo.toml",
        cargo_home,
    )
    return time.monotonic() - started


def new_run_root(runs: Path) -> Path:
    """Create a unique root for one copied boundary verification run.

    Parameters
    ----------
    runs : Path
        Parent directory for run roots. It is created when absent.

    Returns
    -------
    Path
        New directory whose name starts with ``run-``.

    Raises
    ------
    OSError
        If the parent or unique run directory cannot be created.
    """  # ruff: ignore[docstring-extraneous-exception] - filesystem failures propagate from mkdir and mkdtemp.
    runs.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=runs))


@contextlib.contextmanager
def boundary_cargo_run(
    run_root: Path,
    workspace: Path,
    prepare_cargo_home: cabc.Callable[[Path, Path], float],
) -> cabc.Iterator[None]:
    """Record Cargo preparation and workload timings around one run.

    Preparation timing is recorded even when preparation fails. Errors from
    preparation, workload execution, or timing writes propagate to the caller.
    On exit, cleanup removes ``cargo-home`` and ``target`` beneath ``run_root``
    while keeping the root and its ``timings.jsonl`` file. Cleanup errors are
    ignored so they do not replace the workload result.

    Parameters
    ----------
    run_root : Path
        Run directory receiving ``timings.jsonl`` and prepared build state.
    workspace : Path
        Copied workspace passed to ``prepare_cargo_home``.
    prepare_cargo_home : Callable[[Path, Path], float]
        Callable that prepares Cargo state and returns its elapsed duration in
        seconds. It receives ``run_root`` and ``workspace``.

    Yields
    ------
    None
        Control passes to the caller for the timed workload after preparation
        succeeds.

    Raises
    ------
    Exception
        Any exception raised by preparation, the workload, or timing writes
        propagates after the owned Cargo home and target directory are removed.
    """  # ruff: ignore[docstring-extraneous-exception] - callbacks and context-manager work may propagate any exception.
    timings_path = run_root / "timings.jsonl"
    try:
        preparation_started = time.monotonic()
        try:
            prepare_cargo_home(run_root, workspace)
        finally:
            record_phase(
                timings_path,
                "dependency_preparation",
                time.monotonic() - preparation_started,
            )
        workload_started = time.monotonic()
        try:
            yield
        finally:
            record_phase(
                timings_path,
                "workload",
                time.monotonic() - workload_started,
            )
    finally:
        shutil.rmtree(run_root / "cargo-home", ignore_errors=True)
        shutil.rmtree(run_root / "target", ignore_errors=True)
