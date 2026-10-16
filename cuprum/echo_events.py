"""Structured stream-echo events for observability integrations.

A text-only echo sink that cannot represent the subprocess output, or whose
destination has closed under it, stops the echo for its drain while capture
continues. The first failure is an observability fact — a console has gone
quiet — but it is not a command lifecycle event, and
:data:`~cuprum.events.ExecPhase` is a closed set that registered consumers
match exhaustively. Adding a phase would raise inside consumers that were
correct when they were written. A dedicated event type on its own hook registry
(:mod:`cuprum.echo_observation`) means a consumer opts in by registering, and a
consumer that does not is untouched.

The event carries only bounded values. The stream name is one of ``stdout`` or
``stderr`` and the error category is one closed value; a sink's type, its
encoding, any exception text, and the subprocess payload never reach it.

Opt-in bounded delivery also reports saturated queue writes, failed worker
writes or flushes, and output abandoned after the shutdown drain grace. Those
events use the same categorical channel and do not change capture or process
outcomes.

The two recoverable failures are not equally welcome. An unencodable payload is
inherent to the sink's configuration and is always handled. A broken pipe may
instead mean the caller's destination went away, so recovering from it is
:class:`BrokenPipePolicy` — opt-in, defaulting to today's strict propagation.
"""

from __future__ import annotations

import dataclasses as dc
import enum
import typing as typ

from cuprum._enum_parse import parse_enum_member

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
    """Why an echo event occurred, as a closed set of categories.

    Examples
    --------
    The member value is the string operators see::

        assert EchoErrorCategory.UNICODE_ENCODE == "unicode_encode"
        assert EchoErrorCategory.BROKEN_PIPE == "broken_pipe"
        assert EchoErrorCategory.DROPPED == "dropped"
        assert EchoErrorCategory.UNDELIVERED == "undelivered"
        assert EchoErrorCategory.DELIVERY_ERROR == "delivery_error"

    """

    UNICODE_ENCODE = "unicode_encode"
    BROKEN_PIPE = "broken_pipe"
    TRUNCATED = "truncated"
    DROPPED = "dropped"
    UNDELIVERED = "undelivered"
    DELIVERY_ERROR = "delivery_error"


class BrokenPipePolicy(enum.StrEnum):
    """How a drain responds when an echo sink reports a broken pipe.

    A presentation sink writes to a destination the parent does not own — a
    log collector, a console, another process's reader — and that destination
    can close before the child stops writing. The ``BrokenPipeError`` that
    follows is indistinguishable, from inside the drain, from any other sink
    failure, so the caller names the intended response rather than the drain
    guessing.

    Members
    -------
    STRICT
        Propagate ``BrokenPipeError`` out of the drain. This is the default,
        and preserves the behaviour every existing caller already has.
    BEST_EFFORT
        Disable echoing for the affected stream only, so capture, line
        observation, and child reaping continue. The transition is reported
        once through the echo observation channel, the ``cuprum.stream``
        ``WARNING``, and the command's result diagnostics.

    Only ``BrokenPipeError`` is affected. A sink that fails with any other
    ``OSError`` still propagates under both members, so a genuinely
    unreachable device is never mistaken for a closed reader.

    Examples
    --------
    The member value is the string operators see::

        assert BrokenPipePolicy.BEST_EFFORT == "best_effort"

    """

    STRICT = "strict"
    BEST_EFFORT = "best_effort"


def _parse_broken_pipe_policy(raw: BrokenPipePolicy | str) -> BrokenPipePolicy:
    """Coerce a raw policy value to a :class:`BrokenPipePolicy` member.

    This is the pure parsing core behind
    :meth:`cuprum.sh.output.RunOutputOptions.__post_init__`: it takes the raw
    value directly, so a caller that passes the string spelling gets the same
    validation a member does and the failure is raised before a child ever
    spawns.

    Parameters
    ----------
    raw:
        A :class:`BrokenPipePolicy` member or its string value.

    Returns
    -------
    BrokenPipePolicy
        The parsed policy.

    Raises
    ------
    ValueError
        If ``raw`` is not a known policy value.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from parse_enum_member
    return parse_enum_member(BrokenPipePolicy, raw, field="broken_pipe_policy")


@dc.dataclass(frozen=True, slots=True)
class EchoEvent:
    """A stream-echo event reported to registered echo hooks.

    Attributes
    ----------
    stream:
        Which output stream the failing echo belonged to.
    error_category:
        The closed-set category naming why the event occurred.
    dropped_bytes:
        Child-output bytes omitted from a successfully truncated echo line,
        otherwise ``None``. Bounded delivery also uses this field for bytes
        dropped on queue saturation or left undelivered at shutdown.

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
    dropped_bytes: int | None = None


@dc.dataclass(frozen=True, slots=True)
class RelayFallback:
    """One categorical echo failure or delivery-loss record for a stream.

    Some records describe a handled failure that disables echo for the stream,
    such as an unencodable payload or a tolerated broken pipe. Bounded delivery
    also records one ``DROPPED`` event per rejected chunk, groups
    ``UNDELIVERED`` bytes by stream when the worker closes, and reports a
    ``DELIVERY_ERROR`` if a worker write or flush fails. These delivery records
    do not claim that synchronous echo was disabled.

    Attributes
    ----------
    stream:
        Which output stream the failure or lost output belonged to. The value
        is the closed-set :class:`EchoStream`, not free text.
    error_category:
        The closed-set :class:`EchoErrorCategory` naming the failure or loss.
        It is categorical only: the sink's type, its encoding, exception text,
        and subprocess payload never reach the record.

    Examples
    --------
    A stdout echo that could not encode its payload::

        fallback = RelayFallback(
            stream=EchoStream.STDOUT,
            error_category=EchoErrorCategory.UNICODE_ENCODE,
        )
        assert fallback.stream == "stdout"

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
    "BrokenPipePolicy",
    "EchoErrorCategory",
    "EchoEvent",
    "EchoHook",
    "EchoStream",
    "RelayFallback",
]
