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


def rsync_sync(
    source: str | Path,
    destination: str | Path,
    *,
    options: RsyncOptions | None = None,
) -> SafeCmd:
    """Build an `rsync` synchronisation command."""
    resolved_options = options or RsyncOptions()
    args: list[str] = []
    if resolved_options.archive:
        args.append("--archive")
    if resolved_options.delete:
        args.append("--delete")
    if resolved_options.dry_run:
        args.append("--dry-run")
    if resolved_options.verbose:
        args.append("--verbose")
    if resolved_options.compress:
        args.append("--compress")

    args.extend([
        str(safe_path(source, allow_relative=resolved_options.allow_relative)),
        str(
            safe_path(
                destination,
                allow_relative=resolved_options.allow_relative,
            )
        ),
    ])
    return sh.make(RSYNC)(*args)


__all__ = ["RsyncOptions", "rsync_sync"]
