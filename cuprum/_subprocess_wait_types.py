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
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._stream_drain_state import _RelayDiagnostics

# One stream's captured payload: text in the ordinary mode, the child's bytes
# untouched in the byte-exact one.
type _StreamPayload = str | bytes
type _StreamConsumerTask = asyncio.Task[_StreamPayload | None]
type _EofGraceWaiter = cabc.Callable[
    [tuple[_StreamConsumerTask, _StreamConsumerTask]],
    cabc.Awaitable[object],
]


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

    capture: bool
    eof_grace_waiter: _EofGraceWaiter | None = None
    pid: int | None = None
    observation: _StageObservation | None = None
    discard_on_cancel: asyncio.Event | None = None
    capture_bytes: bool = False


@dc.dataclass(frozen=True, slots=True)
class _RunTaskOwnership:
    """The stdin writer, stream consumers, and diagnostics owned by one run.

    ``relay_diagnostics`` holds the per-stream collectors handed to the
    consumer drains, so the run's one reconciliation point — success gather or
    teardown drain — reads the result diagnostics from the tasks it already
    settles rather than inspecting them a second time.
    """

    stdin_task: asyncio.Task[None] | None
    consumers: tuple[_StreamConsumerTask, _StreamConsumerTask]
    discard_on_cancel: asyncio.Event
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics]
    idle: _IdleMonitor | None = None


__all__ = [
    "_DrainContext",
    "_EofGraceWaiter",
    "_RunTaskOwnership",
    "_StreamConsumerTask",
    "_StreamPayload",
]
