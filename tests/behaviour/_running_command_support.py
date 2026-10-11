"""Start and wait orchestration for the handle scenarios.

:mod:`tests.behaviour._running_command_control_support` drives the stop,
signal, and teardown scenarios;
:mod:`tests.behaviour._running_command_children` builds the children and owns
the shared record both return. This module drives start and wait, and every
module carries a leading underscore so pytest does not collect it as a test
module.

Each ``_observe_*`` function runs one scenario's whole story inside a single
event loop and returns a
:class:`~tests.behaviour._running_command_children.HandleObservation`. That
shape is forced by the artefact under test rather than chosen for convenience:
a ``RunningCommand`` holds an :class:`asyncio.Lock` and an asyncio subprocess,
both bound to the loop that spawned them, so a handle cannot be carried across
two ``asyncio.run`` calls. A scenario spanning several steps would have to
re-open a loop mid-story, which is exactly what a handle cannot survive.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum.sh import ExecutionContext, RunOutputOptions, TimeoutExpired
from tests.behaviour._running_command_children import (
    DELAYED_WRITER_MARKER,
    HandleObservation,
    ReadyChild,
    _reaped,
    _wait_for_ready,
)

if typ.TYPE_CHECKING:
    from cuprum.sh import RunningCommand, SafeCmd

__all__ = [
    "DELAYED_WRITER_MARKER",
    "HandleObservation",
    "ReadyChild",
    "_observe_concurrent_waits",
    "_observe_early_exit_signal",
    "_observe_failed_start",
    "_observe_output_wait",
    "_observe_start",
    "_observe_wait_deadline",
    "_start_owned",
]

#: How many waiters the concurrent-wait scenario issues at once.
_WAITERS = 5


async def _start_owned(
    command: SafeCmd,
    context: ExecutionContext | None = None,
) -> RunningCommand:
    """Start *command* and return the handle its outcome is read through.

    ``context`` defaults to the plain execution settings every scenario shares;
    one is supplied only by a scenario that needs a different cancel grace.

    Returns
    -------
    RunningCommand
        The handle to the started command.
    """
    return await command.start(
        output=RunOutputOptions(),
        context=ExecutionContext() if context is None else context,
    )


def _observe_start(child: ReadyChild) -> HandleObservation:
    """Start a blocking child and report what the handle exposed while it ran."""
    marker = child.marker

    async def orchestrate() -> HandleObservation:
        """Read what the handle exposes while its child is still running."""
        handle = await _start_owned(child.command)
        await _wait_for_ready(marker)
        observation = HandleObservation(
            handle_pid=handle.pid,
            exec_id=handle.exec_id,
            child_was_live=handle.pid is not None,
            result=handle.result,
        )
        await handle.kill()
        return dc.replace(observation, child_reaped=await _reaped(handle.pid))

    return asyncio.run(orchestrate())


def _observe_output_wait(command: SafeCmd) -> HandleObservation:
    """Await a child that writes, then ends with its own status."""

    async def orchestrate() -> HandleObservation:
        """Await the child and report the outcome it settled."""
        handle = await _start_owned(command)
        result = await handle.wait()
        return HandleObservation(
            result=result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_failed_start(command: SafeCmd) -> HandleObservation:
    """Start a program that is not on disk and carry out the failure."""

    async def orchestrate() -> HandleObservation:
        """Carry out whatever failure the absent executable's spawn raises."""
        try:
            await _start_owned(command)
        except (FileNotFoundError, OSError) as failure:
            # Carried out rather than handled: which failure reaches the caller
            # is the scenario's subject, so it must not be absorbed here. The
            # set is the spawn failures a missing executable can produce.
            return HandleObservation(error=failure)
        return HandleObservation()

    return asyncio.run(orchestrate())


def _observe_early_exit_signal(
    command: SafeCmd,
    signal_number: int,
) -> HandleObservation:
    """Await a child that exited early, then attempt to signal it.

    The child is waited on first, so the handle has settled before the signal
    is attempted. That ordering is the realistic one: a caller coordinating a
    child that failed to come up learns it exited from the handle's own
    outcome, and only then finds that a signal has nowhere to go.

    Returns
    -------
    HandleObservation
        The settled outcome, and the refusal the signal attempt raised.
    """

    async def orchestrate() -> HandleObservation:
        """Await the early exit, then attempt a signal the handle must refuse."""
        handle = await _start_owned(command)
        result = await handle.wait()
        try:
            await handle.signal(signal_number)
        except ProcessLookupError as refusal:
            # Carried out rather than handled: that the refusal reached the
            # caller, and what it named, is the scenario's subject.
            return HandleObservation(
                result=result,
                error=refusal,
                handle_pid=handle.pid,
                child_reaped=await _reaped(handle.pid),
            )
        return HandleObservation(result=result, handle_pid=handle.pid)

    return asyncio.run(orchestrate())


def _observe_wait_deadline(command: SafeCmd) -> HandleObservation:
    """Wait on a blocking child under an already-elapsed deadline."""

    async def orchestrate() -> HandleObservation:
        """Wait under an elapsed deadline and carry out the expiry."""
        handle = await _start_owned(command)
        try:
            await handle.wait(timeout=0)
        except TimeoutExpired as expiry:
            # Carried out rather than handled: the deadline surfacing as the
            # public timeout is the scenario's subject.
            return HandleObservation(
                error=expiry,
                handle_pid=handle.pid,
                child_reaped=await _reaped(handle.pid),
            )
        return HandleObservation(handle_pid=handle.pid)

    return asyncio.run(orchestrate())


def _observe_concurrent_waits(command: SafeCmd) -> HandleObservation:
    """Issue several waits on one handle at once and report what each saw."""

    async def orchestrate() -> HandleObservation:
        """Issue every wait at once and keep what each of them observed."""
        handle = await _start_owned(command)
        results = await asyncio.gather(*(handle.wait() for _ in range(_WAITERS)))
        return HandleObservation(
            result=results[0],
            results=tuple(results),
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())
