"""Tar command builders with typed argument helpers."""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import sh
from cuprum.builders.args import safe_path
from cuprum.catalogue import TAR

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class TarCreateOptions:
    """Optional flags for tar_create."""

    gzip: bool = False
    bzip2: bool = False
    xz: bool = False
    allow_relative: bool = False


def tar_create(
    archive: str | Path,
    sources: typ.Sequence[str | Path],
    *,
    options: TarCreateOptions | None = None,
) -> SafeCmd:
    """Build a `tar` create command."""
    if not sources:
        msg = "tar_create requires at least one source path"
        raise ValueError(msg)

    resolved_options = options or TarCreateOptions()
    compression_flags = [
        resolved_options.gzip,
        resolved_options.bzip2,
        resolved_options.xz,
    ]
    if sum(1 for flag in compression_flags if flag) > 1:
        msg = "Select only one compression option for tar_create"
        raise ValueError(msg)

    args: list[str] = ["-c"]
    if resolved_options.gzip:
        args.append("-z")
    if resolved_options.bzip2:
        args.append("-j")
    if resolved_options.xz:
        args.append("-J")

    args.extend([
        "-f",
        str(
            safe_path(
                archive,
                allow_relative=resolved_options.allow_relative,
            )
        ),
    ])
    args.extend(
        str(
            safe_path(
                source,
                allow_relative=resolved_options.allow_relative,
            )
        )
        for source in sources
    )
    return sh.make(TAR)(*args)


def tar_extract(
    archive: str | Path,
    *,
    destination: str | Path | None = None,
    allow_relative: bool = False,
) -> SafeCmd:
    """Build a `tar` extract command."""
    args: list[str] = [
        "-x",
        "-f",
        str(safe_path(archive, allow_relative=allow_relative)),
    ]
    if destination is not None:
        args.extend(["-C", str(safe_path(destination, allow_relative=allow_relative))])
    return sh.make(TAR)(*args)


__all__ = ["TarCreateOptions", "tar_create", "tar_extract"]
