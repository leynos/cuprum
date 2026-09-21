#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Install the checksum-verified PyPy 8 Python 3.12 linter binary.

The Makefile provides the pinned release URL, digest, and installation paths
through environment variables. Keeping those values at the call site preserves
Make overrides while moving download and extraction behaviour into testable
Python code.
"""

from __future__ import annotations

import dataclasses
import os
import platform
import tarfile
from pathlib import Path

from scripts.install_boundary_kani import checked_download


@dataclasses.dataclass(frozen=True, slots=True)
class PyPyInstallation:
    """Describe the pinned release and the destination it must provide."""

    url: str
    digest: str
    archive: Path
    root: Path
    python: Path


def _required_environment(name: str) -> str:
    """Return one required installer setting or fail with its variable name."""
    value = os.environ.get(name)
    if value is None:
        msg = f"{name} is required to install the PyPy linter"
        raise ValueError(msg)
    return value


def installation_from_environment() -> PyPyInstallation:
    """Construct an installation request from the Makefile's pinned variables."""
    return PyPyInstallation(
        url=_required_environment("PYPY312_URL"),
        digest=_required_environment("PYPY312_SHA256"),
        archive=Path(_required_environment("PYPY312_ARCHIVE_PATH")),
        root=Path(_required_environment("PYPY312_ROOT")),
        python=Path(_required_environment("PYPY312_PYTHON")),
    )


def _require_linux_x86_64() -> None:
    """Reject platforms without the pinned prebuilt PyPy archive."""
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        msg = "PyPy 8.0.0 Python 3.12 linting is supported only on Linux x86_64"
        raise RuntimeError(msg)


def _is_executable(path: Path) -> bool:
    """Report whether `path` is an executable regular file."""
    return path.is_file() and os.access(path, os.X_OK)


def _extract(archive: Path, destination: Path) -> None:
    """Extract a trusted release archive without accepting unsafe members."""
    with tarfile.open(archive, "r:gz") as release:
        release.extractall(destination, filter="data")


def install(installation: PyPyInstallation) -> None:
    """Download, verify, and extract PyPy unless the matching binary exists."""
    _require_linux_x86_64()
    installation.root.parent.mkdir(parents=True, exist_ok=True)
    checked_download(installation.url, installation.archive, installation.digest)
    if installation.root.is_dir():
        if not _is_executable(installation.python):
            msg = "PyPy extraction is incomplete; remove .pypy and retry"
            raise RuntimeError(msg)
    else:
        _extract(installation.archive, installation.root.parent)
    if not _is_executable(installation.python):
        msg = "PyPy extraction did not provide the expected executable"
        raise RuntimeError(msg)


def main() -> None:
    """Install the PyPy interpreter configured by the invoking Makefile."""
    install(installation_from_environment())


if __name__ == "__main__":
    main()
