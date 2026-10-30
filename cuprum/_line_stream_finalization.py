"""Teardown for a line stream, split from the iterator that drives it.

``SafeCmd.lines()`` has more ways to end than ``run()``: completion, a timeout,
a caller's ``break`` followed by an explicit close, and cancellation all reach
the same finalizer. That finalizer reconciles the coordinator, settles the
observe hooks, and maps how iteration ended onto the presentation session's
terminal outcome — one cohesive job, but a large enough one to push
:mod:`cuprum._line_iteration` past the module ceiling.

Nothing here decides *whether* to tear down; :func:`_iter_line_events` owns
that. This module only decides how, so the ordering it enforces — reconcile the
child, drain the hooks, then release the session — lives in one readable place.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._observability import (
    _drain_tasks_during_cleanup,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_types import _EventDetails
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._sink_lifecycle import _outcome_for_error, _outcome_for_result
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.events import TerminalOutcome
from cuprum.sinks import base as sinks

if typ.TYPE_CHECKING:
    from cuprum._execution_tracking import _ExecutionTracking
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import CommandResult

_LINES_FINALIZATION_ERROR = "line stream finalization failed"


@dc.dataclass(frozen=True, slots=True)
class _LineStreamFinalization:
    """Facts needed to settle a line stream after its coordinator exits."""

    execution: _SubprocessExecution
    result: CommandResult | None
    failure: BaseException | None


def _line_stream_outcome(
    result: CommandResult | None,
    failure: BaseException | None,
) -> sinks.SessionOutcome:
    """Map how a line stream ended onto its session's terminal outcome.

    Returns
    -------
    sinks.SessionOutcome
        The terminal report for the adapter. An exit carries its code; a
        ``GeneratorExit`` or cancellation reports ``CANCELLED`` and no code.

    Notes
    -----
    Reaching both ``None`` means a close landed before the run produced either
    outcome. That is cancellation for the same reason an explicitly closed
    stream is: the caller asked to stop, and no child ever reported.
    """
    if failure is not None:
        return _outcome_for_error(failure)
    if result is not None:
        return _outcome_for_result(result)
    return sinks.SessionOutcome(outcome=TerminalOutcome.CANCELLED)


def _emit_line_stream_error(
    execution: _SubprocessExecution,
    result: CommandResult | None,
    failure: BaseException | None,
) -> None:
    """Settle a line stream after its coordinator has reconciled the child."""
    if failure is None:
        return
    outcome = _outcome_for_error(failure)
    is_cancelled = outcome.outcome is TerminalOutcome.CANCELLED
    _safe_emit_terminal(
        execution.observation,
        outcome.outcome,
        _EventDetails(
            pid=(
                None
                if is_cancelled
                else result.pid
                if result is not None
                else execution.observation.started_pid
            ),
            exit_code=(None if is_cancelled or result is None else result.exit_code),
            duration_s=None if result is None else result.duration,
        ),
    )


async def _reconcile_line_stream(
    coordinator: asyncio.Task[None] | None,
    result_future: asyncio.Future[CommandResult],
    tracking: _ExecutionTracking,
    finalization: _LineStreamFinalization,
) -> None:
    """Cancel the coordinator if running, then drain its observe-hook tasks.

    Completion, cancellation, or ``aclose()`` runs this and reconciles the
    hooks exactly once; a bare ``break`` closes nothing, so nothing here
    runs. A failure before the task was created leaves ``coordinator``
    ``None``, and the drain still covers hooks a plan emit had queued.
    """
    if coordinator is not None and not result_future.done():
        coordinator.cancel()
    try:
        if coordinator is not None:
            await _shielded_cleanup(_absorb_coordinator(coordinator, result_future))
    except BaseException as error:
        # A published coordinator failure must not skip observe-hook cleanup:
        # the active error groups a failing hook with the outcome, not over it.
        _emit_line_stream_error(
            finalization.execution,
            finalization.result,
            finalization.failure,
        )
        await _shielded_cleanup(_drain_line_stream_tasks(tracking.pending_tasks, error))
        raise
    _emit_line_stream_error(
        finalization.execution,
        finalization.result,
        finalization.failure,
    )
    await _shielded_cleanup(
        _drain_line_stream_tasks(tracking.pending_tasks, finalization.failure)
    )


async def _absorb_coordinator(
    coordinator: asyncio.Task[None],
    result_future: asyncio.Future[CommandResult],
) -> None:
    """Wait the coordinator out, re-raising a published failure.

    A cancelled coordinator is the ordinary shape of teardown — the iterator
    cancels it once it has stopped consuming — so its cancellation is absorbed
    rather than re-raised, and ``aclose()`` tears the child down without
    surfacing a ``CancelledError`` the caller never issued.
    """
    await asyncio.gather(coordinator, return_exceptions=True)
    if result_future.done() and not result_future.cancelled():
        error = result_future.exception()
        if error is not None:
            raise error


async def _drain_line_stream_tasks(
    pending: list[asyncio.Task[None]],
    failure: BaseException | None,
) -> None:
    """Reconcile observe-hook tasks on every way iteration can end.

    ``lines()`` has more exits than ``run()`` — a caller ``break`` and a
    generator close join completion, timeout, and cancellation — so the drain
    sits in one place instead of on the success path only. Iteration ending on
    a failure aggregates a drain failure with it, so a background hook cannot
    stand in for the error that ended the run. A ``GeneratorExit`` is not a
    failure in that sense, and closing a stream must not raise a group pairing
    it with an unrelated hook error.
    """
    if failure is None or isinstance(failure, GeneratorExit):
        await _wait_for_exec_hook_tasks(pending)
        return
    await _drain_tasks_during_cleanup(
        pending,
        failure,
        message=_LINES_FINALIZATION_ERROR,
    )


__all__ = [
    "_LineStreamFinalization",
    "_line_stream_outcome",
    "_reconcile_line_stream",
]
