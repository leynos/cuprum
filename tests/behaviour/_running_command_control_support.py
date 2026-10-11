"""Stop, signal, and teardown orchestration for the handle scenarios.

The companion module :mod:`tests.behaviour._running_command_support` drives the
start and wait scenarios, and
:mod:`tests.behaviour._running_command_children` builds the children both
drive. Both are imported by :mod:`tests.behaviour.test_running_command`, which
owns the assertions, and both carry a leading underscore so pytest does not
collect them as test modules.

The split is by subject rather than by size. A scenario that stops a child has
to arrange a child that blocks, wait for it to be genuinely running, act, and
then confirm it was reaped — a different sequence from starting a child and
reading its outcome. Keeping the two apart means neither module's helpers
branch on which kind of scenario is asking.

Every ``_observe_*`` function returns the shared
:class:`~tests.behaviour._running_command_children.HandleObservation`, so the
``Then`` steps in the test module need not know which module produced a record.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from tests.behaviour._running_command_children import (
    HandleObservation,
    ReadyChild,
    _reaped,
    _wait_for_ready,
)
from tests.behaviour._running_command_support import _start_owned

if typ.TYPE_CHECKING:
    import pytest

    from cuprum.sh import ExecutionContext, RunningCommand

__all__ = [
    "_observe_cancelled_teardown",
    "_observe_context_exit",
    "_observe_context_exit_with_error",
    "_observe_escalating_termination",
    "_observe_graceful_termination",
    "_observe_kill",
    "_observe_repeated_termination",
    "_observe_unsupported_signal",
]

#: Grace allowed for a child that ignores ``SIGTERM`` to be escalated.
_ESCALATION_GRACE = 0.2

#: How long the cancelled-teardown scenario lets the stop run before it
#: interrupts. Long enough to be inside the escalation window, short enough
#: that the scenario does not wait out the full grace.
_CANCEL_AFTER = 0.3

#: Grace the cancelled-teardown child is given, so its escalation is still
#: pending when the cancellation lands.
_CANCEL_GRACE = 1.5


async def _start_ready(child: ReadyChild) -> RunningCommand:
    """Start *child*'s command and wait until its process is genuinely running."""
    handle = await _start_owned(child.command)
    await _wait_for_ready(child.marker)
    return handle


def _observe_graceful_termination(child: ReadyChild) -> HandleObservation:
    """Terminate a child that obeys ``SIGTERM`` and report the outcome."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        result = await handle.terminate()
        return HandleObservation(
            result=result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_escalating_termination(child: ReadyChild) -> HandleObservation:
    """Terminate a child that ignores ``SIGTERM`` under a short grace."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        result = await handle.terminate(grace=_ESCALATION_GRACE)
        return HandleObservation(
            result=result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_kill(child: ReadyChild) -> HandleObservation:
    """Compel a child to exit and report the signal that ended it."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        result = await handle.kill()
        return HandleObservation(
            result=result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_repeated_termination(child: ReadyChild) -> HandleObservation:
    """Terminate one handle twice and report both outcomes."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        first = await handle.terminate()
        second = await handle.terminate()
        return HandleObservation(
            result=first,
            results=(first, second),
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_context_exit(child: ReadyChild) -> HandleObservation:
    """Leave the handle's context while the child is still running."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        async with handle:
            pass
        return HandleObservation(
            result=handle.result,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _observe_context_exit_with_error(child: ReadyChild) -> HandleObservation:
    """Leave the handle's context with the block already failing."""

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        error: BaseException | None = None
        try:
            async with handle:
                _raise_block_error()
        except ValueError as raised:
            # Carried out rather than handled: whether the block's own error
            # survives teardown is exactly what the scenario asserts.
            error = raised
        return HandleObservation(
            result=handle.result,
            error=error,
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _raise_block_error() -> None:
    """Raise the error a scenario's block fails with.

    Raised from a helper rather than inline so the ``raise`` does not sit
    inside the ``try`` whose handler the scenario is testing.

    Raises
    ------
    ValueError
        Always, carrying the message the scenario asserts survives teardown.
    """
    msg = "block error"
    raise ValueError(msg)


def _observe_cancelled_teardown(child: ReadyChild) -> HandleObservation:
    """Cancel the task leaving a handle's context mid-teardown, then report it.

    The child ignores ``SIGTERM`` and the grace is generous, so the stop is
    still waiting out its escalation window when the cancellation lands. That
    puts the interruption inside teardown rather than before it, which is the
    ordering this scenario exists to pin: a stop interrupted by cancellation
    must still reap the child and record an outcome.

    Returns
    -------
    HandleObservation
        The settled outcome, and whether the interruption left a child behind.
    """

    async def orchestrate() -> HandleObservation:
        from cuprum.sh import ExecutionContext as Context

        context: ExecutionContext = Context(cancel_grace=_CANCEL_GRACE)
        handle = await child.command.start(context=context)
        await _wait_for_ready(child.marker)

        async def leave_context() -> None:
            async with handle:
                await asyncio.sleep(30)

        task = asyncio.create_task(leave_context())
        await asyncio.sleep(_CANCEL_AFTER)
        # Keep cancelling until the task is finished: teardown runs shielded,
        # so the first cancellation starts the wait rather than ending it, and
        # a fresh cancellation is what the scenario is about.
        while not task.done():
            task.cancel()
            await asyncio.sleep(0)
        return HandleObservation(
            result=handle.result,
            error=_task_failure(task),
            handle_pid=handle.pid,
            child_reaped=await _reaped(handle.pid),
        )

    return asyncio.run(orchestrate())


def _task_failure(task: asyncio.Task[None]) -> BaseException | None:
    """Return the task's own failure, or ``None`` when it only cancelled.

    Returns
    -------
    BaseException | None
        The exception the task raised, or ``None`` for a task that ended by
        cancellation or returned normally.
    """
    if task.cancelled() or task.exception() is None:
        return None
    return task.exception()


def _observe_unsupported_signal(
    child: ReadyChild,
    signal_number: int,
    monkeypatch: pytest.MonkeyPatch,
) -> HandleObservation:
    """Signal a live child on a platform that cannot deliver the signal.

    The platform predicate is replaced for the duration of the call, so the
    refusal is exercised on the platform the suite runs on rather than only on
    the one it is missing from. The patch names the delivery module rather than
    the error leaf, because that is the module whose globals resolve the
    predicate: patching the leaf would leave the real check in place and the
    scenario would pass while testing nothing.

    Parameters
    ----------
    child : ReadyChild
        The child to signal, already running.
    signal_number : int
        The signal to ask for.
    monkeypatch : pytest.MonkeyPatch
        Fixture that installs the replacement predicate, and undoes it once the
        scenario's teardown has finished with it.

    Returns
    -------
    HandleObservation
        The refusal that reached the caller, and the child's reaping.
    """

    async def orchestrate() -> HandleObservation:
        handle = await _start_ready(child)
        monkeypatch.setattr(
            "cuprum._process_handle.control._arbitrary_signals_supported",
            _platform_cannot_deliver,
        )
        try:
            await handle.signal(signal_number)
        except RuntimeError as refusal:
            observation = HandleObservation(error=refusal, handle_pid=handle.pid)
        else:
            observation = HandleObservation(handle_pid=handle.pid)
        return dc.replace(observation, child_reaped=await _stop_and_reap(handle))

    return asyncio.run(orchestrate())


def _platform_cannot_deliver() -> bool:
    """Report that this platform cannot deliver a caller-named signal."""
    return False


async def _stop_and_reap(handle: RunningCommand) -> bool:
    """Stop *handle* so a scenario leaves no child behind, and report it gone.

    Returns
    -------
    bool
        Whether the child had gone by the time this returned.
    """
    if handle.result is None:
        await handle.kill()
    return await _reaped(handle.pid)
