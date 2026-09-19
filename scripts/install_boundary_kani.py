#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Install pinned Kani frontend binaries and fetch its verified native bundle.

The approved frontend is the cargo-quickinstall artefact also pinned by
Netsuke. The upstream bundle supplies CBMC and the matching Rust compiler.
``make install-boundary-kani`` performs the subsequent native setup command;
this script never invokes Cargo or compiles a verifier from source.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import tarfile
import urllib.parse
from pathlib import Path

# The repository root the installer reads its version pin from and writes its
# cache beneath. `CUPRUM_BOUNDARY_ROOT` redirects both, so a test or a staging
# wrapper can install into an isolated tree without touching the checkout.
_ROOT_ENV = "CUPRUM_BOUNDARY_ROOT"

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.67.0"
TARGET = "x86_64-unknown-linux-gnu"
FRONTEND_NAME = f"kani-verifier-{VERSION}-{TARGET}.tar.gz"
BUNDLE_NAME = f"kani-{VERSION}-{TARGET}.tar.gz"
FRONTEND_MEMBERS = ("cargo-kani", "kani")
FRONTEND_DIGEST = "ed2bafc239b834e14c6b66fc4838e342e3bc0b814e548e72ea30e84f83dc0974"
BUNDLE_DIGEST = "3b5f7afd3b51603ee720db7bc1bc4fe46b5a4f5d36daad9939c4b4c658b51ac0"
FRONTEND_URL = (
    "https://github.com/cargo-bins/cargo-quickinstall/releases/download/"
    f"kani-verifier-{VERSION}/{FRONTEND_NAME}"
)
BUNDLE_URL = (
    "https://github.com/model-checking/kani/releases/download/"
    f"kani-{VERSION}/{BUNDLE_NAME}"
)


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


def boundary_root() -> Path:
    """Return the repository root whose pins and cache this installer uses.

    Honours ``CUPRUM_BOUNDARY_ROOT`` so an isolated tree — a test fixture, or a
    staging wrapper — can be installed into without modifying the checkout the
    script lives in.

    Returns
    -------
    Path
        The configured root, or the repository containing this script.
    """
    override = os.environ.get(_ROOT_ENV)
    return Path(override) if override else ROOT


def _require_current_pin(pin: str) -> None:
    """Refuse a VERSION file that no longer matches the packaged digests."""
    if pin != VERSION:
        msg = "update Kani archive names and digests with its version pin"
        raise ValueError(msg)


def _extract_frontend(archive: Path, destination: Path) -> None:
    """Extract exactly the approved frontend executables into the cache."""
    destination.mkdir(exist_ok=True)
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(
            destination,
            members=[bundle.getmember(name) for name in FRONTEND_MEMBERS],
            filter="data",
        )


def main() -> None:
    """Install the pinned frontend into a private verification tool directory."""
    root = boundary_root()
    pin = (root / "tools/kani/VERSION").read_text(encoding="utf-8").strip()
    _require_current_pin(pin)
    cache = root / ".cache/boundary-kani"
    cache.mkdir(parents=True, exist_ok=True)
    checked_download(FRONTEND_URL, cache / FRONTEND_NAME, FRONTEND_DIGEST)
    checked_download(BUNDLE_URL, cache / BUNDLE_NAME, BUNDLE_DIGEST)
    _extract_frontend(cache / FRONTEND_NAME, cache / "bin")


if __name__ == "__main__":
    main()
