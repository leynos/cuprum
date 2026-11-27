"""Settling a byte handle, and publishing its live readers' outcomes.

A byte run settles through the same coordinator, delivery-worker close, and
result fields as a text run. What it adds is the live half: the queues must be
released before reconciliation so a stalled consumer cannot hold teardown open,
each reader must publish one terminal state, and a drain failure discovered at
reconciliation must reach the caller instead of hiding behind a short capture.

The settlement task is also the supervisor. It races the child's exit against
the stdin writer and both stream consumers, so a reader that dies while the
child is still writing ends the run rather than waiting for an exit that may
never come. The race's precedence is the rendezvous module's, not restated
here: a settled exit, then the writer, then stdout, then stderr.
"""

from __future__ import annotations

import functools
import typing as typ

from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._process_handle.coordinator import (
    _build_handle_result,
    _finalize_settled_run,
    _HandleOutcome,
    _ReconcileHooks,
)
from cuprum._process_handle.settlement import _expiry_error, _SettlementPlan
from cuprum._process_handle.tasks import _HandleRun, _launch_idle_monitor
from cuprum._process_handle_errors import LiveStreamError
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_startup_abandon import _release_output_transports
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _wait_for_exit_code_within_timeout,
)
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._byte_stream.wiring import _ByteStreams
    from cuprum._subprocess_wait_types import _RunTaskOwnership
    from cuprum.sh import BytesCommandResult

__all__ = ["bytes_settlement_plan"]

_STREAM_NAMES = (EchoStream.STDOUT, EchoStream.STDERR)


def bytes_settlement_plan(
    streams: _ByteStreams,
) -> _SettlementPlan[BytesCommandResult]:
    """Build the settlement plan a byte handle runs with.

    Parameters
    ----------
    streams : _ByteStreams
        The live queues and readers this handle owns.

    Returns
    -------
    _SettlementPlan[BytesCommandResult]
        The plan that drives a byte run to its result.
    """
    return _SettlementPlan(
        settle=functools.partial(_settle_bytes_handle_run, streams=streams),
        expiry=_expiry_error,
    )


async def _settle_bytes_handle_run(
    run: _HandleRun,
    streams: _ByteStreams,
) -> BytesCommandResult:
    """Wait for the child, supervise its streams, and assemble the byte result.

    The wait races the stdin writer and both consumers, so an early stream
    failure stops the child through the exit wait's cancellation. Every exit
    path releases the live queues and publishes a terminal state on each
    reader; the success path additionally releases the output transports and
    closes the delivery worker before the result is built.

    Returns
    -------
    BytesCommandResult
        The command's outcome, carrying the child's bytes untouched.

    Raises
    ------
    LiveStreamError
        If a stream consumer failed, whether before the exit or during the
        final drain. The failed stream is published as ``FAILED`` and the
        other as ``TRUNCATED_BY_SETTLEMENT``.
    BaseException
        Whatever else ended the run: a deadline, a cancellation, or a producer
        failure. Readers that did not reach a clean end are truncated.
    """  # ruff: ignore[docstring-extraneous-exception] - the monitored failures propagate
    execution = run.execution
    _launch_idle_monitor(execution)
    try:
        exit_code, exited_at = await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(run.process, execution),
            run.ownership.stdin_task,
            consumers=run.ownership.consumers,
            stdin_log=run.ownership.stdin_log,
        )
    except BaseException as error:
        failure = _publish_early_failure(run.ownership, streams, error)
        try:
            await _finalize_settled_run(
                run,
                execution,
                preserve=False,
                hooks=_ReconcileHooks(before=streams.begin_settlement),
            )
        finally:
            streams.finalize_settlement()
        if failure is error:
            raise
        raise failure from error

    try:
        drained = await _finalize_settled_run(
            run,
            execution,
            preserve=True,
            hooks=_ReconcileHooks(
                before=streams.begin_settlement,
                after=functools.partial(
                    _release_output_transports,
                    run.process,
                    execution,
                ),
            ),
        )
    except BaseException:
        streams.finalize_settlement()
        raise

    late_failure = _late_consumer_failure(run.ownership)
    if late_failure is not None:
        streams.fail(late_failure[0], late_failure[1])
    streams.finalize_settlement()
    if late_failure is not None:
        index, cause = late_failure
        raise LiveStreamError(_STREAM_NAMES[index], cause=cause)

    # Handle results and after-hooks must see write failures and undelivered
    # bytes discovered only when the shared delivery worker settles, so the
    # worker closes before the outcome is built.
    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        )
    )
    # Imported here, not at module scope: ``cuprum._bytes_run`` imports the
    # ``cuprum.sh`` result modules, and this module is reached while that
    # package is still initializing.
    from cuprum._bytes_run import _require_bytes_command_result

    return _require_bytes_command_result(
        _build_handle_result(
            run,
            _HandleOutcome(
                exit_code=exit_code,
                exited_at=exited_at,
                stdout_payload=drained[0].payload,
                stderr_payload=drained[1].payload,
                captures=(drained[0].capture, drained[1].capture),
            ),
            capture_bytes=True,
        )
    )


def _publish_early_failure(
    ownership: _RunTaskOwnership,
    streams: _ByteStreams,
    error: BaseException,
) -> BaseException:
    """Publish the readers' states for a failure that ended the wait early.

    A consumer's own failure is re-raised as ``LiveStreamError`` and marks that
    stream ``FAILED``. A producer failure keeps its ``StdinSourceError`` type
    and every reader is truncated, because the output streams themselves did
    not fail. Anything else — a cancellation, a deadline, an OS error — also
    truncates rather than inventing a stream failure.

    Returns
    -------
    BaseException
        The error the settlement should raise.
    """
    index = _consumer_index(ownership, error)
    if index is None:
        streams.begin_settlement()
        return error
    streams.fail(index, error)
    return LiveStreamError(_STREAM_NAMES[index], cause=error)


def _consumer_index(
    ownership: _RunTaskOwnership,
    error: BaseException,
) -> int | None:
    """Return which consumer raised *error* by identity, if one did.

    Identity is the only reliable provenance test: a callback can raise
    ``TimeoutError`` itself, and a type check would confuse that with the run's
    deadline. The rendezvous raises the very object the task carries, so the
    comparison is exact.

    Returns
    -------
    int | None
        ``0`` for stdout, ``1`` for stderr, or ``None`` when no consumer
        raised *error*.
    """
    for index, task in enumerate(ownership.consumers):
        if _task_failure(task) is error:
            return index
    return None


def _late_consumer_failure(
    ownership: _RunTaskOwnership,
) -> tuple[int, BaseException] | None:
    """Return a consumer's non-cancellation failure found at reconciliation.

    The reconciliation gathers consumers with ``return_exceptions=True`` so a
    failed reader cannot mask the run's own error. That also means a reader
    that failed during the final drain would otherwise be reported as an empty
    capture, so the failure is re-read here and given precedence over the
    successful result.

    Returns
    -------
    tuple[int, BaseException] | None
        The stream index and the failure, or ``None`` when no consumer failed.
    """
    for index, task in enumerate(ownership.consumers):
        failure = _task_failure(task)
        if failure is not None:
            return index, failure
    return None


def _task_failure(task: asyncio.Task[object] | None) -> BaseException | None:
    """Return a settled consumer's own failure, ignoring cancellation.

    Returns
    -------
    BaseException | None
        The exception the task ended on, or ``None`` for a task that succeeded,
        is still running, was cancelled, or does not exist.
    """
    if task is None or not task.done():
        return None
    if task.cancelled():
        return None
    return task.exception()
