"""Refusing a standard-stream binding that could not be honoured.

Split from ``cuprum._stdio_plan`` when the second of these guards pushed that
module past the 400-line ceiling. The seam is a single responsibility rather
than a line count: ``cuprum._stdio_plan`` decides *which* binding each target
resolves to, and this module decides whether that binding is one the child can
actually be given.

Both refusals exist because a binding that looks plausible to the caller can be
a lie by the time it reaches ``subprocess``. A platform without POSIX
descriptors cannot redirect a stream at all, and a negative descriptor is a
``subprocess`` sentinel rather than a descriptor — for an *output* stream, one
that has ``subprocess`` build a pipe cuprum never reads, leaving the child to
block on a full buffer. Neither is visible in the resolved value: the first is
a fact about the platform, and the second is a fact about how ``subprocess``
reads the integer. So both are decided here, where the target is still in hand
and the failure can name the stream and the source it came from.
"""

from __future__ import annotations

import os
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._constants import PipeStream

__all__ = [
    "_ensure_redirection_supported",
    "_reject_negative_descriptor",
]


def _ensure_redirection_supported(stream: PipeStream, kind: str) -> None:
    """Refuse descriptor redirection where the platform cannot honour it.

    Only the descriptor arms are gated. Inheriting a stream is handled by
    ``Popen`` itself on every platform, and a library-owned pipe is an ordinary
    ``os.pipe``, so neither is a POSIX-specific claim and neither is refused
    here — a supported combination must keep working on a platform that cannot
    redirect.

    Parameters
    ----------
    stream : str
        The stream being redirected, for the message.
    kind : str
        The target variant being refused.

    Raises
    ------
    ValueError
        If redirection is attempted on a platform without POSIX descriptors.
    """
    if os.name == "posix":
        return
    msg = (
        f"RunOutputOptions {stream} cannot be redirected to {kind!r} on this "
        "platform: cuprum opens and closes standard-stream descriptors with "
        "POSIX semantics, which it does not have here. Leave stdout and stderr "
        "unset on this platform."
    )
    raise ValueError(msg)


def _reject_negative_descriptor(
    stream: PipeStream,
    descriptor: int,
    *,
    resource: str,
) -> None:
    """Refuse a subprocess sentinel masquerading as a borrowed descriptor.

    ``subprocess`` reads negative integers as sentinels rather than
    descriptors: ``-1`` is ``PIPE``, ``-2`` is ``STDOUT``, and ``-3`` is
    ``DEVNULL``. A binding that names one would be a lie — for an *output*
    stream, ``-1`` makes ``subprocess`` create a pipe that cuprum neither owns
    nor reads, so the child blocks on a full buffer and the run hangs. Both
    routes into a descriptor land here: an ``int`` payload, and whatever a
    caller's ``fileno()`` returned.

    Parameters
    ----------
    stream : str
        ``"stdout"`` or ``"stderr"``, named in the refusal.
    descriptor : int
        The value about to be handed to ``subprocess``.
    resource : str
        How the descriptor was obtained, for the message: ``"int"`` for a
        literal payload, otherwise the file object's type name.

    Raises
    ------
    ValueError
        If the descriptor is negative.
    """
    if descriptor >= 0:
        return
    msg = (
        f"RunOutputOptions {stream} resolved to the descriptor "
        f"{descriptor} from {resource}; negative values are subprocess "
        "sentinels (-1 is PIPE, -2 is STDOUT, -3 is DEVNULL), which would "
        "have subprocess act on the stream itself rather than bind this one. "
        "Pass a non-negative descriptor."
    )
    raise ValueError(msg)
