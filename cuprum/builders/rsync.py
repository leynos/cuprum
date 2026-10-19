"""Rsync command builders with typed argument helpers."""

from __future__ import annotations

import dataclasses as dc
import os
import typing as typ

from cuprum import sh
from cuprum.builders.args import SafePath, safe_path
from cuprum.catalogue import RSYNC

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class RsyncOptions:
    """Optional flags for the rsync builder.

    Attributes
    ----------
    allow_relative
        Accept relative local operands. This never permits remote syntax: both
        operands must remain local paths, and ``host:path``, ``host::module``,
        and ``rsync://`` spellings are rejected whether or not this is set.
    """

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


_REMOTE_SPELLING_HINT = (
    "remote syntax such as host:path, host::module or rsync:// is not supported"
)


def _is_remote_rsync_operand(operand: str) -> bool:
    """Report whether ``operand`` uses rsync's remote endpoint grammar.

    Rsync reads a colon before the first path separator as a remote
    specification. A later colon, as in ``dir/host:path``, is part of the local
    name and stays local.

    Parameters
    ----------
    operand:
        Operand string to inspect.

    Returns
    -------
    bool
        ``True`` when rsync would treat the operand as a remote endpoint.
    """
    if operand.lower().startswith("rsync://"):
        return True
    colon = operand.find(":")
    slash = operand.find("/")
    return colon != -1 and (slash == -1 or colon < slash)


def _local_rsync_operand(
    value: str | Path,
    *,
    role: str,
    allow_relative: bool,
) -> SafePath:
    """Validate one rsync operand as a local path and return its normalization.

    ``safe_path`` supplies the generic checks. This helper then rejects remote
    spelling in both the raw value and the normalized result, so neither
    representation can smuggle a remote endpoint past the sink.

    Parameters
    ----------
    value:
        Operand to validate.
    role:
        Operand role, either ``"source"`` or ``"destination"``, used in the
        rejection message.
    allow_relative:
        When True, relative local operands are permitted.

    Returns
    -------
    SafePath
        Normalized local operand.

    Raises
    ------
    ValueError
        If ``value`` is not a valid absolute local path (or a relative one
        when ``allow_relative`` is set), or if it spells a remote endpoint.
    """
    checked = safe_path(value, allow_relative=allow_relative)
    raw_value = os.fspath(value)
    if _is_remote_rsync_operand(raw_value) or _is_remote_rsync_operand(checked):
        msg = f"rsync {role} must be a local path; {_REMOTE_SPELLING_HINT}"
        raise ValueError(msg)
    return checked


def _rsync_argv(
    source: str | Path,
    destination: str | Path,
    options: RsyncOptions,
) -> tuple[str, ...]:
    """Build the immutable argv for ``rsync_sync`` without a catalogue.

    ``--`` follows the generated flags and precedes the source and
    destination, so a path beginning with ``-`` (or ``--``) cannot be parsed
    as an rsync option. rsync still interprets a remote specification such as
    ``host:path`` in the operand position; that is rsync's own path syntax,
    not option parsing, and is unchanged by the separator.

    Returns
    -------
    tuple[str, ...]
        The complete rsync argument vector.
    """
    flags = tuple(flag for attr, flag in _FLAG_ORDER if getattr(options, attr))
    paths = (
        str(
            _local_rsync_operand(
                source,
                role="source",
                allow_relative=options.allow_relative,
            )
        ),
        str(
            _local_rsync_operand(
                destination,
                role="destination",
                allow_relative=options.allow_relative,
            )
        ),
    )
    return (*flags, "--", *paths)


def rsync_sync(
    source: str | Path,
    destination: str | Path,
    *,
    options: RsyncOptions | None = None,
) -> SafeCmd:
    """Build an ``rsync`` synchronization command.

    Both operands are local paths. Remote rsync syntax is rejected for either
    operand, including when ``RsyncOptions.allow_relative`` is set; a remote
    transfer needs a custom builder.

    Parameters
    ----------
    source
        Local source path to synchronize from. It follows a ``--`` separator,
        so a leading dash cannot turn it into an option.
    destination
        Local destination path to synchronize to, likewise separated by
        ``--``.
    options
        Optional rsync settings. When omitted, the command uses
        ``RsyncOptions()``.

    Returns
    -------
    SafeCmd
        Safe command wrapper for the curated ``rsync`` program and generated
        argv.
    """
    argv = _rsync_argv(source, destination, options or RsyncOptions())
    return sh.make(RSYNC)(*argv)


__all__ = ["RsyncOptions", "rsync_sync"]
