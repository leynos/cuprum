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


@dc.dataclass(frozen=True, slots=True)
class TarCreateOptions:
    """Optional flags for tar_create.

    Attributes
    ----------
    compression : Compression
        Compression algorithm for the archive. Defaults to
        ``Compression.NONE``.
    allow_relative : bool
        Allow relative archive and source paths. This applies to both archive
        and source paths. Defaults to ``False``.
    """

    compression: Compression = Compression.NONE
    allow_relative: bool = False


def _get_compression_flag(options: TarCreateOptions) -> str:
    """Return the compression flag for tar_create, or ``""`` for none."""
    match options.compression:
        case Compression.NONE:
            return ""
        case Compression.GZIP:
            return "-z"
        case Compression.BZIP2:
            return "-j"
        case Compression.XZ:
            return "-J"
        case never:
            typ.assert_never(never)


def _tar_create_argv(
    archive: str | Path,
    sources: cabc.Sequence[str | Path],
    options: TarCreateOptions,
) -> tuple[str, ...]:
    """Build the immutable argv for ``tar_create`` without a catalogue.

    The ``--`` separator is emitted after the archive value so that
    leading-dash source names are file operands, never tar options. Without
    it, GNU tar parses a source such as ``--checkpoint=1`` as an option and a
    paired ``--checkpoint-action=exec=...`` as a command to run with the
    caller's privileges.

    Returns
    -------
    tuple[str, ...]
        The complete tar argument vector.

    Raises
    ------
    TypeError
        If ``sources`` is a single ``str`` or ``Path`` rather than a sequence.
    ValueError
        If ``sources`` is empty.
    """
    match sources:
        case str() | Path():
            msg = "tar_create requires a sequence of source paths, not a single path"
            raise TypeError(msg)
    if not sources:
        msg = "tar_create requires at least one source path"
        raise ValueError(msg)

    compression_flag = _get_compression_flag(options)
    args: list[str] = ["-c"]
    if compression_flag:
        args.append(compression_flag)

    args.extend([
        "-f",
        str(safe_path(archive, allow_relative=options.allow_relative)),
        "--",
    ])
    args.extend(
        str(safe_path(source, allow_relative=options.allow_relative))
        for source in sources
    )
    return tuple(args)


def tar_create(
    archive: str | Path,
    sources: cabc.Sequence[str | Path],
    *,
    options: TarCreateOptions | None = None,
) -> SafeCmd:
    """Build a ``tar`` create command.

    Parameters
    ----------
    archive
        Archive path passed after ``-f``.
    sources
        Source paths added to the archive, in order. They follow a ``--``
        separator, so a name beginning with ``-`` stays a file operand and
        cannot become a tar option.
    options
        Optional create settings. When omitted, the command uses
        ``TarCreateOptions()``.

    Returns
    -------
    SafeCmd
        Safe command wrapper for the curated ``tar`` program and generated
        argv.
    """
    argv = _tar_create_argv(archive, sources, options or TarCreateOptions())
    return sh.make(TAR)(*argv)


def _tar_extract_argv(
    archive: str | Path,
    destination: str | Path | None,
    *,
    allow_relative: bool,
) -> tuple[str, ...]:
    """Build the immutable argv for ``tar_extract`` without a catalogue.

    No ``--`` separator is needed here: the archive and destination are
    values of ``-f`` and ``-C``, so tar takes them literally even when they
    begin with a dash. A ``--`` *before* an option's value would instead make
    tar read the value as a file operand.

    Returns
    -------
    tuple[str, ...]
        The complete tar argument vector.
    """
    args: list[str] = [
        "-x",
        "-f",
        str(safe_path(archive, allow_relative=allow_relative)),
    ]
    if destination is not None:
        args.extend(["-C", str(safe_path(destination, allow_relative=allow_relative))])
    return tuple(args)


def tar_extract(
    archive: str | Path,
    *,
    destination: str | Path | None = None,
    allow_relative: bool = False,
) -> SafeCmd:
    """Build a ``tar`` extract command.

    Parameters
    ----------
    archive
        Archive path passed as the value of ``-f``. Leading dashes are not
        re-read as options. A bare ``-`` is still special: tar reads the
        archive from standard input.
    destination
        Optional extraction directory passed as the value of ``-C``.
    allow_relative
        Allow ``archive`` and ``destination`` to be relative paths.

    Returns
    -------
    SafeCmd
        Safe command wrapper for the curated ``tar`` program and generated
        argv.
    """
    argv = _tar_extract_argv(archive, destination, allow_relative=allow_relative)
    return sh.make(TAR)(*argv)


__all__ = ["Compression", "TarCreateOptions", "tar_create", "tar_extract"]
