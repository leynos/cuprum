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

import pytest

from cuprum.sh import ExecutionContext, RunOutputOptions, TimeoutExpired
from tests.behaviour._running_command_children import (
    DELAYED_WRITER_MARKER,
    HandleObservation,
    ReadyChild,
    _reaped,
    _wait_for_ready,
    _wait_until_exited,
    live_child_pids,
)
from tests.helpers.process_state import process_state

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
    "_observe_unwaited_exit_signal",
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


def _new_children_since(before: frozenset[int] | None) -> tuple[frozenset[int], bool]:
    """Read the child listing again and report what appeared, if anything.

    The availability flag is derived from the readings themselves rather than
    from a capability probe taken beforehand. A host that can enumerate can
    still fail the enumeration, and a probe that ran before it would report
    that host as one where the comparison was made — leaving a failed scan
    indistinguishable from a library that leaked nothing.

    Returns
    -------
    tuple[frozenset[int], bool]
        The PIDs that appeared, and whether both readings answered. The set is
        empty when they did not, which a caller must read only once the flag
        says the comparison was actually made.
    """
    after = live_child_pids()
    if before is None or after is None:
        return frozenset(), False
    return after - before, True


def _observe_failed_start(command: SafeCmd) -> HandleObservation:
    """Start a program that is not on disk and carry out the failure.

    A failed start reports no PID — there is no child to report — so the PID
    field carries no evidence about orphans. What does carry evidence is the
    set of processes this test process has as children: it is sampled before
    the attempt and again after, and a spawn that leaked would leave the before
    set a strict subset of the after set.

    Whether that comparison was made is recorded alongside it rather than
    assumed. A reading that could not be taken is not an empty set of children,
    so the ``Then`` step asserts the availability flag before it reads the
    difference as proof.

    Returns
    -------
    HandleObservation
        The failure the spawn raised, the child PIDs that appeared while the
        attempt ran, and whether both readings behind them answered.
    """

    async def orchestrate() -> HandleObservation:
        """Carry out whatever failure the absent executable's spawn raises."""
        before = live_child_pids()
        try:
            await _start_owned(command)
        except (FileNotFoundError, OSError) as failure:
            # Carried out rather than handled: which failure reaches the caller
            # is the scenario's subject, so it must not be absorbed here. The
            # set is the spawn failures a missing executable can produce.
            spawned, available = _new_children_since(before)
            return HandleObservation(
                error=failure,
                spawned_children=spawned,
                child_listing_available=available,
            )
        spawned, available = _new_children_since(before)
        return HandleObservation(
            spawned_children=spawned,
            child_listing_available=available,
        )

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


def _skip_if_unobservable_exit(handle: RunningCommand) -> None:
    """End the scenario as not applicable where this host cannot see the exit.

    The scenario's subject is the window between a child's exit and its reaping,
    and that window is only observable on a host that can report a process as
    exited while it remains listed. Without procfs there is no way to wait for
    the window to open, so a signal issued anyway would race the child's own
    start-up and pass or fail for the wrong reason. Skipping says that honestly,
    which a vacuous pass would not.

    Ends the scenario through *pytest.skip*, which raises rather than returns,
    so there is nothing to describe under a returns heading.
    """
    pid = handle.pid
    if pid is None or not process_state(pid).available:
        pytest.skip("this host cannot observe an unreaped child's exit")


def _observe_unwaited_exit_signal(
    command: SafeCmd,
    signal_number: int,
) -> HandleObservation:
    """Signal a child that exited while its handle was only held.

    The window under test sits between the child's exit and the reaper that
    would publish its status, and no wait may cross it: the existing
    early-exit scenario waits first, which settles the handle and publishes
    the code before the signal is attempted. Here the handle is held and
    nothing else, so a refusal that reports the child's real status cannot
    have read it from the handle. That is what
    :attr:`HandleObservation.unsettled_when_refused` records, read at the
    moment of the refusal rather than afterwards — settling the handle to
    find out would close the very window being tested.

    Returns
    -------
    HandleObservation
        The refusal, the handle's own unsettled state at that instant, and
        whether the child was reaped once the scenario stopped it.
    """

    async def orchestrate() -> HandleObservation:
        """Signal a merely-held handle whose child has already exited."""
        handle = await _start_owned(command)
        _skip_if_unobservable_exit(handle)
        pid = typ.cast("int", handle.pid)
        await _wait_until_exited(pid)
        try:
            await handle.signal(signal_number)
        except ProcessLookupError as refusal:
            # Carried out rather than handled: that the refusal reached the
            # caller, and what it named, is the scenario's subject. The
            # unsettled flag is read here, before anything settles the handle.
            observation = HandleObservation(
                error=refusal,
                unsettled_when_refused=handle.result is None,
                handle_pid=pid,
            )
        else:
            observation = HandleObservation(handle_pid=pid)
        # The child outlived the refusal because nothing else stopped it, so
        # the scenario stops it here and only then observes that it has gone.
        await handle.kill()
        return dc.replace(observation, child_reaped=await _reaped(pid))

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
