"""Tar command builders with typed argument helpers."""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.builders.args import safe_path
from cuprum.catalogue import TAR

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


class Compression(enum.Enum):
    """Compression algorithm for ``tar_create``.

    Modelling compression as a single enum makes the "exactly one algorithm"
    invariant unrepresentable as an invalid state: the previous design used
    three mutually-exclusive booleans that had to be reconciled at runtime.

    Members
    -------
    NONE
        No compression (the default); ``tar_create`` emits no flag.
    GZIP
        gzip compression (``-z``).
    BZIP2
        bzip2 compression (``-j``).
    XZ
        xz compression (``-J``).
    """

    NONE = "none"
    GZIP = "gzip"
    BZIP2 = "bzip2"
    XZ = "xz"


_COMPRESSION_FLAGS: dict[Compression, str] = {
    Compression.NONE: "",
    Compression.GZIP: "-z",
    Compression.BZIP2: "-j",
    Compression.XZ: "-J",
}


@dc.dataclass(frozen=True, slots=True)
class TarCreateOptions:
    """Optional flags for tar_create.

    The allow_relative flag applies to both archive and source paths.
    """

    compression: Compression = Compression.NONE
    allow_relative: bool = False


def _get_compression_flag(options: TarCreateOptions) -> str:
    """Return the compression flag for tar_create, or ``""`` for none."""
    return _COMPRESSION_FLAGS[options.compression]


def tar_create(
    archive: str | Path,
    sources: cabc.Sequence[str | Path],
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


__all__ = ["Compression", "TarCreateOptions", "tar_create", "tar_extract"]
