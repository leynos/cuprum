"""Rsync command builders with typed argument helpers."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import sh
from cuprum.builders.args import safe_path
from cuprum.catalogue import RSYNC

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class RsyncOptions:
    """Optional flags for the rsync builder."""

    archive: bool = False
    delete: bool = False
    dry_run: bool = False
    verbose: bool = False
    compress: bool = False
    allow_relative: bool = False


# Flag emission order is part of the argv contract: option attributes are
# rendered in this sequence, ahead of the source and destination paths.
_FLAG_ORDER: tuple[tuple[str, str], ...] = (
    ("archive", "--archive"),
    ("delete", "--delete"),
    ("dry_run", "--dry-run"),
    ("verbose", "--verbose"),
    ("compress", "--compress"),
)


def _rsync_argv(
    source: str | Path,
    destination: str | Path,
    options: RsyncOptions,
) -> tuple[str, ...]:
    """Build the immutable argv for ``rsync_sync`` without a catalogue."""
    flags = tuple(flag for attr, flag in _FLAG_ORDER if getattr(options, attr))
    paths = (
        str(safe_path(source, allow_relative=options.allow_relative)),
        str(safe_path(destination, allow_relative=options.allow_relative)),
    )
    return flags + paths


def rsync_sync(
    source: str | Path,
    destination: str | Path,
    *,
    options: RsyncOptions | None = None,
) -> SafeCmd:
    """Build an ``rsync`` synchronisation command.

    Parameters
    ----------
    source
        Source path to synchronise from.
    destination
        Destination path to synchronise to.
    options
        Optional rsync settings. When omitted, the command uses
        ``RsyncOptions()``.

    Returns
    -------
    SafeCmd
        Safe command wrapper for the curated ``rsync`` program and generated
        argv.

    Raises
    ------
    ValueError
        If ``source`` or ``destination`` is relative while relative paths are
        not allowed.
    """
    argv = _rsync_argv(source, destination, options or RsyncOptions())
    return sh.make(RSYNC)(*argv)


__all__ = ["RsyncOptions", "rsync_sync"]
