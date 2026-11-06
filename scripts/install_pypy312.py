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

This module also owns the small HTTPS downloader used for the PyPy archive.
Verifier binaries no longer use it: Kani and Z3 installs are delegated to the
shared `rust-prover-tools` dependency, which applies byte ceilings and a
transfer deadline before the digest check.
"""

from __future__ import annotations

import dataclasses
import hashlib
import http.client
import os
import platform
import shutil
import tarfile
import urllib.parse
from pathlib import Path


def checked_download(url: str, destination: Path, digest: str) -> None:
    """Fetch an HTTPS artefact and fail closed on any checksum mismatch.

    Parameters
    ----------
    url : str
        HTTPS release asset URL.
    destination : Path
        Local cache path; an existing file is reused only if its digest matches.
    digest : str
        Expected SHA-256 digest from the approved release manifest.

    Raises
    ------
    ValueError
        If the URL is not HTTPS or downloaded bytes do not match the pin.
    OSError
        If an existing cache entry cannot be inspected, or the download cannot
        be transferred into the cache.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _cache_is_current and _digest.
    if not url.startswith("https://"):
        msg = "binary downloads require HTTPS"
        raise ValueError(msg)
    if _cache_is_current(destination, digest):
        return
    pending = destination.with_suffix(".download")
    try:
        _download(url, pending)
        _require_digest(pending, destination, digest)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _cache_is_current(destination: Path, digest: str) -> bool:
    """Report whether an existing cache entry already matches the pin.

    A cache entry that cannot be read is not a cache miss. The failure is
    raised, not reported as ``False``, so ``checked_download`` never answers an
    unreadable cache with a fresh download that would overwrite it.

    Parameters
    ----------
    destination : Path
        Local cache path to inspect.
    digest : str
        Expected SHA-256 digest from the approved release manifest.

    Returns
    -------
    bool
        ``True`` only for a readable entry whose digest already matches.

    Raises
    ------
    OSError
        If the cache entry exists but cannot be opened or read.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _digest.
    try:
        current = _digest(destination)
    except FileNotFoundError:
        return False
    return current == digest


def _require_digest(pending: Path, destination: Path, digest: str) -> None:
    """Refuse downloaded bytes that do not match the approved digest."""
    if _digest(pending) != digest:
        msg = f"binary checksum mismatch: {destination.name}"
        raise ValueError(msg)


def _download(url: str, destination: Path) -> None:
    """Follow bounded HTTPS-only release redirects into a temporary file."""
    for _ in range(6):
        redirect = _download_once(url, destination)
        if redirect is None:
            return
        url = redirect
    msg = "release redirect limit exceeded"
    raise ValueError(msg)


def _connect(url: str) -> tuple[http.client.HTTPSConnection, str]:
    """Validate one hop's URL and open its HTTPS connection."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        msg = "binary downloads and redirects require HTTPS"
        raise ValueError(msg)
    target = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, "")) or "/"
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=60)
    return connection, target


def _redirect_url(url: str, response: http.client.HTTPResponse) -> str | None:
    """Resolve a redirect target or validate a terminal download response."""
    if response.status in {301, 302, 303, 307, 308}:
        location = response.getheader("Location")
        if location is None:
            msg = "release redirect has no Location"
            raise ValueError(msg)
        return urllib.parse.urljoin(url, location)
    if response.status != http.client.OK:
        msg = f"release download failed: HTTP {response.status}"
        raise OSError(msg)
    return None


def _fetch_body(
    connection: http.client.HTTPSConnection,
    url: str,
    destination: Path,
) -> str | None:
    """Save the response body, or resolve one redirect hop instead."""
    with connection.getresponse() as response:
        redirect = _redirect_url(url, response)
        if redirect is not None:
            return redirect
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output)
        return None


def _download_once(url: str, destination: Path) -> str | None:
    """Close one HTTPS connection after saving its body or resolving a redirect."""
    connection, target = _connect(url)
    try:
        connection.request(
            "GET",
            target,
            headers={"User-Agent": "cuprum-boundary-verification"},
        )
        return _fetch_body(connection, url, destination)
    finally:
        connection.close()


def _digest(path: Path) -> str:
    """Hash a release artefact without loading the bundle into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@dataclasses.dataclass(frozen=True, slots=True)
class PyPyInstallation:
    """Describe the pinned release and the destination it must provide.

    Attributes
    ----------
    url:
        HTTPS URL of the pinned prebuilt PyPy release archive.
    digest:
        Expected SHA-256 digest of that archive, checked before extraction.
    archive:
        Local cache path the verified archive is downloaded into.
    root:
        Destination directory the archive's top-level tree is extracted to.
    python:
        Interpreter path that must exist and be executable once installed.
    """

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
    """Construct an installation request from the Makefile's pinned variables.

    Returns
    -------
    PyPyInstallation
        The request described by ``PYPY312_URL``, ``PYPY312_SHA256``,
        ``PYPY312_ARCHIVE_PATH``, ``PYPY312_ROOT``, and ``PYPY312_PYTHON``.

    Raises
    ------
    ValueError
        If any of those variables is unset.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from _required_environment.
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


def _ensure_extracted(installation: PyPyInstallation) -> None:
    """Extract the verified archive, or reject a root it cannot repair.

    A destination that exists without the expected interpreter is the residue
    of an extraction that failed partway, and unpacking over it would not
    restore the missing files, so the operator is told to remove the
    directory and retry instead.

    Raises
    ------
    RuntimeError
        If the destination directory exists without the expected executable.
    """
    if installation.root.is_dir():
        if not _is_executable(installation.python):
            msg = "PyPy extraction is incomplete; remove .pypy and retry"
            raise RuntimeError(msg)
    else:
        _extract(installation.archive, installation.root.parent)


def install(installation: PyPyInstallation) -> None:
    """Download, verify, and extract PyPy unless the matching binary exists.

    Parameters
    ----------
    installation : PyPyInstallation
        Pinned release and destination. A destination that already holds an
        executable interpreter is reused without touching the archive.

    Raises
    ------
    RuntimeError
        On a platform other than Linux x86_64, when an existing destination
        holds no executable interpreter, or when extraction fails to produce
        the expected executable.
    ValueError
        If the downloaded archive does not match the pinned digest.
    OSError
        If the archive cannot be downloaded or the destination cannot be read
        or created.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError and OSError propagate from checked_download.
    _require_linux_x86_64()
    installation.root.parent.mkdir(parents=True, exist_ok=True)
    if installation.root.is_dir() and _is_executable(installation.python):
        return
    checked_download(installation.url, installation.archive, installation.digest)
    _ensure_extracted(installation)
    if not _is_executable(installation.python):
        msg = "PyPy extraction did not provide the expected executable"
        raise RuntimeError(msg)


def main() -> None:
    """Install the PyPy interpreter configured by the invoking Makefile.

    Raises
    ------
    ValueError
        If the invoking environment omits a required pinning variable.
    RuntimeError
        If the platform is unsupported or installation fails to produce the
        expected executable.
    OSError
        If the archive cannot be downloaded or the destination cannot be read
        or created.
    """  # ruff: ignore[docstring-extraneous-exception] - these propagate from installation_from_environment and install.
    install(installation_from_environment())


if __name__ == "__main__":
    main()
