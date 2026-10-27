"""Derive cache locations and configuration keys for Cargo snapshots."""

from __future__ import annotations

import dataclasses as dc
import hashlib
import os
import platform
from pathlib import Path

_SNAPSHOT_INPUTS = (Path("rust/Cargo.lock"), Path("rust/rust-toolchain.toml"))


@dc.dataclass(frozen=True, slots=True)
class _RunnerScope:
    """Identify the runner dimensions used to scope a Cargo cache snapshot."""

    operating_system: str | None = None
    architecture: str | None = None
    environment: str | None = None
    generation: str | None = None


def default_snapshot_root() -> Path:
    """Return the per-user Cargo snapshot directory for every build route.

    Returns
    -------
    Path
        The versioned snapshot-cache directory under ``XDG_CACHE_HOME`` or
        the user's default cache directory.
    """
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return cache_home / "cuprum" / "cargo-snapshots-v2"


def cargo_snapshot_key(
    repository_root: Path,
    cargo_version: str,
    runner: _RunnerScope | None = None,
) -> str:
    """Return a content key for the lockfile, toolchain, Cargo, and runner.

    Parameters
    ----------
    repository_root : Path
        Repository containing the Rust lockfile and toolchain file.
    cargo_version : str
        Complete Cargo version string used to prepare the snapshot.
    runner : _RunnerScope, optional
        Runner dimensions to include; unspecified dimensions use the host
        environment.

    Returns
    -------
    str
        Stable directory name derived from the inputs and runner scope.

    Raises
    ------
    OSError
        If either input file cannot be read.
    """  # ruff: ignore[docstring-extraneous-exception] - file read errors propagate from Path.read_bytes.
    runner_scope = runner or _RunnerScope()
    generation = runner_scope.generation or os.environ.get("CACHE_GENERATION", "v1")
    file_hashes = [
        hashlib
        .sha256((repository_root / relative_path).read_bytes())
        .hexdigest()
        .encode()
        for relative_path in _SNAPSHOT_INPUTS
    ]
    file_hashes.append(hashlib.sha256(cargo_version.encode()).hexdigest().encode())
    content_hash = hashlib.sha256(b"".join(file_hashes)).hexdigest()
    scope = "-".join((
        generation,
        runner_scope.operating_system or os.environ.get("RUNNER_OS", platform.system()),
        runner_scope.architecture or os.environ.get("RUNNER_ARCH", platform.machine()),
        runner_scope.environment or os.environ.get("RUNNER_ENVIRONMENT", "local"),
    ))
    return f"cargo-{scope}-{content_hash}"


def _sha256(path: Path) -> str:
    """Hash a lockfile without exposing its contents in diagnostics."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
