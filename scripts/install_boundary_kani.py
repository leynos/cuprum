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

import hashlib
import http.client
import shutil
import tarfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = "0.67.0"
TARGET = "x86_64-unknown-linux-gnu"
FRONTEND_NAME = f"kani-verifier-{VERSION}-{TARGET}.tar.gz"
BUNDLE_NAME = f"kani-{VERSION}-{TARGET}.tar.gz"
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
    """
    if not url.startswith("https://"):
        msg = "binary downloads require HTTPS"
        raise ValueError(msg)
    if destination.exists() and _digest(destination) == digest:
        return
    pending = destination.with_suffix(".download")
    try:
        _download(url, pending)
        if _digest(pending) != digest:
            msg = f"binary checksum mismatch: {destination.name}"
            raise ValueError(msg)
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _download(url: str, destination: Path) -> None:
    """Follow bounded HTTPS-only release redirects into a temporary file."""
    for _ in range(6):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            msg = "binary downloads and redirects require HTTPS"
            raise ValueError(msg)
        connection = http.client.HTTPSConnection(
            parsed.hostname, parsed.port, timeout=60
        )
        try:
            path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, ""))
            connection.request(
                "GET",
                path or "/",
                headers={"User-Agent": "cuprum-boundary-verification"},
            )
            with connection.getresponse() as response:
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location")
                    if location is None:
                        msg = "release redirect has no Location"
                        raise ValueError(msg)
                    url = urllib.parse.urljoin(url, location)
                    continue
                if response.status != http.client.OK:
                    msg = f"release download failed: HTTP {response.status}"
                    raise OSError(msg)
                with destination.open("wb") as output:
                    shutil.copyfileobj(response, output)
                return
        finally:
            connection.close()
    msg = "release redirect limit exceeded"
    raise ValueError(msg)


def _digest(path: Path) -> str:
    """Hash a release artefact without loading the bundle into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def main() -> None:
    """Install the pinned frontend into a private verification tool directory."""
    pin = (ROOT / "tools/kani/VERSION").read_text(encoding="utf-8").strip()
    if pin != VERSION:
        msg = "update Kani archive names and digests with its version pin"
        raise ValueError(msg)
    cache = ROOT / ".cache/boundary-kani"
    cache.mkdir(parents=True, exist_ok=True)
    checked_download(FRONTEND_URL, cache / FRONTEND_NAME, FRONTEND_DIGEST)
    checked_download(BUNDLE_URL, cache / BUNDLE_NAME, BUNDLE_DIGEST)
    frontend = cache / "bin"
    frontend.mkdir(exist_ok=True)
    with tarfile.open(cache / FRONTEND_NAME, "r:gz") as archive:
        archive.extractall(
            frontend,
            members=[archive.getmember("cargo-kani"), archive.getmember("kani")],
            filter="data",
        )


if __name__ == "__main__":
    main()
