"""Structured stream-echo events for observability integrations.

A text-only echo sink that cannot encode the subprocess output stops the echo
for its drain while capture continues. The first failure is an observability
fact — a console has gone quiet — but it is not a command lifecycle event, and
:data:`~cuprum.events.ExecPhase` is a closed set that registered consumers
match exhaustively. Adding a phase would raise inside consumers that were
correct when they were written. A dedicated event type on its own hook registry
(:mod:`cuprum.echo_observation`) means a consumer opts in by registering, and a
consumer that does not is untouched.

The event carries only bounded values. The stream name is one of ``stdout`` or
``stderr`` and the error category is one closed value; a sink's type, its
encoding, any exception text, and the subprocess payload never reach it.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class EchoStream(enum.StrEnum):
    """Which subprocess output stream an echo event describes.

    A closed set rather than free text: operators aggregate the ``stream``
    metric label, so an unbounded sink object name would create one series per
    sink type and a caller-controlled value would be a disclosure risk.

    Examples
    --------
    The member value is the string operators see::

        assert EchoStream.STDOUT == "stdout"

    """

    STDOUT = "stdout"
    STDERR = "stderr"


class EchoErrorCategory(enum.StrEnum):
    """Why an echo write failed, as a closed set of categories.

    Examples
    --------
    The member value is the string operators see::

        assert EchoErrorCategory.UNICODE_ENCODE == "unicode_encode"

    """

    UNICODE_ENCODE = "unicode_encode"


@dc.dataclass(frozen=True, slots=True)
class EchoEvent:
    """A stream-echo failure reported to registered echo hooks.

    Attributes
    ----------
    stream:
        Which output stream the failing echo belonged to.
    error_category:
        The closed-set category naming why the echo write failed.

    Examples
    --------
    A stdout echo that could not encode its payload::

        event = EchoEvent(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.UNICODE_ENCODE,
        )
        assert event.stream == "stdout"

    """

    stream: EchoStream
    error_category: EchoErrorCategory


type EchoHook = cabc.Callable[[EchoEvent], None]
"""A synchronous consumer of :class:`EchoEvent` values.

Echo hooks are synchronous by contract: the emission site sits inline in the
echo write path, between two writes to the same sink, and there is no point at
which awaiting a hook could be ordered against the chunk being echoed.
"""


__all__ = [
    "EchoErrorCategory",
    "EchoEvent",
    "EchoHook",
    "EchoStream",
]
