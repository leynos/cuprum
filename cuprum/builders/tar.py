"""Tar command builders with typed argument helpers."""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.builders.args import safe_path
from cuprum.catalogue import TAR

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class TarCreateOptions:
    """Optional flags for tar_create.

    The allow_relative flag applies to both archive and source paths.
    """

    gzip: bool = False
    bzip2: bool = False
    xz: bool = False
    allow_relative: bool = False


def _get_compression_flag(options: TarCreateOptions) -> str:
    """Return the compression flag for tar_create, if any."""
    compression_flags = [
        options.gzip,
        options.bzip2,
        options.xz,
    ]
    if sum(1 for flag in compression_flags if flag) > 1:
        msg = "Select only one compression option for tar_create"
        raise ValueError(msg)
    if options.gzip:
        return "-z"
    if options.bzip2:
        return "-j"
    if options.xz:
        return "-J"
    return ""


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
    if isinstance(sources, (str, Path)):
        msg = "tar_create requires a sequence of source paths, not a single path"
        raise TypeError(msg)

    resolved_options = options or TarCreateOptions()
    compression_flag = _get_compression_flag(resolved_options)

    args: list[str] = ["-c"]
    if compression_flag:
        args.append(compression_flag)

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
