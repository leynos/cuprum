"""The types one run's waiting and draining halves exchange.

Kept in their own module so the wait half (``cuprum._subprocess_wait``) and the
drain half (``cuprum._stream_drain``) can share them without importing each
other, and so ``cuprum._streams`` — which the wait half already imports the
drain entry point from — never has to reach into the wait module for a type.

``_StreamPayload`` is deliberately wide. Before byte-exact capture existed a
captured stream was always text, so the drain's annotations were ``str``;
sharing one drain between the two modes costs that guarantee, and the type
says so rather than pretending the mode is invisible. The narrow types are
re-established where each result is built, in :mod:`cuprum._result_assembly`.

The pair types are deliberately distinct rather than one abbreviated
annotation used twice. ``_StreamPayloadPair`` is what a caller reporting a
*timeout* needs, and ``_DrainPair`` is what a caller building a *result* needs,
and a run's two halves each pass through both. Spelling them apart means a
helper that narrowed to payloads on the way to a result cannot be mistaken for
one that did not: the record, and with it the spill file's handle, would be
dropped silently.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._stream_drain_finish import _CapturedDrain, _DrainParts

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._stream_drain_state import _RelayDiagnostics
    from cuprum.capture_policy import CapturePolicy

# One stream's captured payload: text in the ordinary mode, the child's bytes
# untouched in the byte-exact one.
type _StreamPayload = str | bytes
# A consumer settles to a whole drain result, record and handle included, so
# the teardown that awaits one already holds everything the caller's result
# needs. It used to be typed as a bare payload, which is what let the record
# be discarded at the reconcile seam without anything noticing.
type _StreamConsumerTask = asyncio.Task[_CapturedDrain]
# A stream cuprum holds no pipe for — one the caller redirected to a file, a
# borrowed descriptor, or the parent's own stream — carries no consumer task.
# The slot is kept rather than removed, because the drain's results are read
# positionally: dropping a slot would shift stderr's payload into stdout's.
type _Consumer = _StreamConsumerTask | None
type _ConsumerPair = tuple[_Consumer, _Consumer]
# One stream's settled payload, in the pair order every waiter uses: stdout
# first, then stderr. Spelled once because both the line-stream teardown and
# the stream-run waiter hand back this pair, and a reader should not have to
# work out from two separate annotations whether the order means anything.
type _StreamPayloadPair = tuple[_StreamPayload | None, _StreamPayload | None]
# The same pair before it is narrowed: what every reconcile hands back, in the
# same stdout-then-stderr order.
type _DrainPair = tuple[_CapturedDrain, _CapturedDrain]
type _EofGraceWaiter = cabc.Callable[[_ConsumerPair], cabc.Awaitable[object]]


@dc.dataclass(frozen=True, slots=True)
class _DrainContext:
    """Capture and observability context for one consumer drain.

    ``capture_bytes`` decides what an *absent* consumer result becomes. A
    cancelled, failed, or never-started reader still owes its caller the empty
    value of the mode the run is in, so the fallback is ``b""`` for a
    byte-exact run and ``""`` for a text one; getting that wrong would hand a
    ``bytes``-typed result field a ``str``. It is read only on those fallback
    paths, because a reader that produced a payload already reports it in the
    mode its config carried.
    """

    # The `(stdout, stderr)` policies this drain is preserving, or
    # ``(None, None)`` on a route that discards output rather than spending
    # the capture grace on text nobody will read. Per stream rather than one
    # flag, because the two streams can be configured differently and the
    # empty fallback an absent reader owes its caller is per stream too.
    captures: tuple[CapturePolicy | None, CapturePolicy | None]
    eof_grace_waiter: _EofGraceWaiter | None = None
    pid: int | None = None
    observation: _StageObservation | None = None
    discard_on_cancel: asyncio.Event | None = None
    capture_bytes: bool = False

    @property
    def capturing(self) -> bool:
        """Whether any stream on this route is being preserved."""
        return any(policy is not None for policy in self.captures)


@dc.dataclass(frozen=True, slots=True)
class _RunTaskOwnership:
    """The stdin writer, stream consumers, and diagnostics owned by one run.

    ``relay_diagnostics`` holds the per-stream collectors handed to the
    consumer drains, so the run's one reconciliation point — success gather or
    teardown drain — reads the result diagnostics from the tasks it already
    settles rather than inspecting them a second time.

    ``captures`` holds the accumulators those consumers are draining into, in
    the same stdout-then-stderr order as ``consumers``. They are here so that
    teardown can reach a spill file that no drain result was ever built for:
    a drain cancelled while awaiting its line sink re-raises without
    finalizing, so nothing travels out with a result and the sink is the only
    remaining reference. It is the normal path that travels out through
    :class:`cuprum._stream_drain_finish._CapturedDrain`; this is the backstop,
    and the two close the same file through one shared
    :class:`cuprum._capture_types.SpillHandle`, so whichever runs first the
    other is a no-op.
    """

    stdin_task: asyncio.Task[None] | None
    consumers: _ConsumerPair
    discard_on_cancel: asyncio.Event
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]
    idle: _IdleMonitor | None = None
    captures: tuple[CaptureSink | None, CaptureSink | None] = (None, None)


__all__ = [
    "_Consumer",
    "_ConsumerPair",
    "_DrainContext",
    "_DrainPair",
    "_DrainParts",
    "_EofGraceWaiter",
    "_RunTaskOwnership",
    "_StreamConsumerTask",
    "_StreamPayload",
    "_StreamPayloadPair",
]
