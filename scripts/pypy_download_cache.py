"""Stage a download beside its cache entry and publish it only once verified.

A transfer writes its bytes to a staging file beside the destination and moves
that file into the cache only after the digest matches. Each transfer reserves
a name no other transfer shares, so two installers racing on one cache path
cannot truncate or unlink each other's bytes. A killed transfer can leave a
staging file behind, so each reservation first sweeps siblings old enough that
no live transfer, whose own file is bounded by the transfer deadline, can still
own them.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

_SUFFIX = ".download"
_TOKEN_BYTES = 8
_ATTEMPTS = 4


class ChecksumMismatchError(ValueError):
    """A completed body did not match the approved digest."""


def stage(destination: Path, *, stale_after_seconds: float) -> Path:
    """Reserve a staging file no other transfer shares.

    The name keeps every suffix the destination carries and adds a random
    token, so `x.tar.gz` and `x.tar.bz2` stage apart from each other and two
    installers racing on one cache path cannot truncate or unlink each other's
    bytes. `O_EXCL` turns the negligible chance of a repeated token into a
    retry rather than a shared file, and the create mode is masked by the
    process umask exactly as `Path.open` would mask it.

    Parameters
    ----------
    destination : Path
        Cache path the caller will publish to.
    stale_after_seconds : float
        Age at which an abandoned staging sibling is debris rather than a
        file a live transfer still owns.

    Returns
    -------
    Path
        An existing, empty file this transfer alone owns.

    Raises
    ------
    OSError
        If no free staging name could be reserved after a few attempts.
    """
    discard_stale(destination, older_than_seconds=stale_after_seconds)
    prefix = f"{destination.name}."
    for _ in range(_ATTEMPTS):
        pending = destination.with_name(
            f"{prefix}{secrets.token_hex(_TOKEN_BYTES)}{_SUFFIX}"
        )
        try:
            handle = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        os.close(handle)
        return pending
    msg = f"could not reserve a staging file beside {destination.name}"
    raise OSError(msg)


def discard_stale(destination: Path, *, older_than_seconds: float) -> None:
    """Remove staging files a killed transfer left beside `destination`.

    A live transfer's staging file is bounded by the transfer deadline, so only
    siblings past `older_than_seconds` are removed and one process never deletes
    another's bytes. File times are wall-clock, so the age gate reads that clock
    rather than any monotonic one the download measures its deadline on. A
    sibling that vanishes or cannot be inspected between the listing and the
    removal belongs to a live transfer, so its error is ignored rather than
    failing this download.
    """
    cutoff = time.time() - older_than_seconds
    prefix = f"{destination.name}."
    try:
        siblings = list(destination.parent.iterdir())
    except FileNotFoundError:
        return
    for sibling in siblings:
        is_staging = sibling.name.startswith(prefix) and sibling.name.endswith(_SUFFIX)
        if not is_staging:
            continue
        try:
            if sibling.stat().st_mtime < cutoff:
                sibling.unlink(missing_ok=True)
        except OSError:
            continue


def publish(pending: Path, destination: Path, digest: str) -> None:
    """Verify the staged bytes, then move them into the cache.

    Raises
    ------
    ChecksumMismatchError
        If the staged bytes do not match the approved digest.
    """
    if digest_of(pending) != digest:
        msg = f"binary checksum mismatch: {destination.name}"
        raise ChecksumMismatchError(msg)
    pending.replace(destination)


def digest_of(path: Path) -> str:
    """Hash a release artefact without loading the bundle into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()
