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

from cuprum._process_handle.control import _terminate_shielded
from cuprum._process_handle.coordinator import (
    _close_handle_delivery,
    _finalize_settled_run,
    _ReconcileHooks,
)
from cuprum._process_handle.results import (
    _assemble_handle_result,
    _handle_outcome,
)
from cuprum._process_handle.settlement import _expiry_error, _SettlementPlan
from cuprum._process_handle.tasks import _HandleRun, _launch_idle_monitor
from cuprum._process_handle_errors import LiveStreamError
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_rendezvous import _stdin_writer_failure
from cuprum._subprocess_startup_abandon import _release_output_transports
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _wait_for_exit_code_within_timeout,
)
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    from cuprum._byte_stream.wiring import _ByteStreams
    from cuprum._subprocess_wait_types import _RunTaskOwnership
    from cuprum.sh import BytesCommandResult

__all__ = ["_stop_failed_child", "bytes_settlement_plan"]

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


async def _stop_failed_child(run: _HandleRun) -> None:
    """Stop and reap the child after a live consumer publishes a failure.

    A reader iterating a live stream has not asked for an outcome, so nothing
    else would stop the child when its drain fails. The stop takes the run's
    lock, so it is serialized with every public control operation and with
    settlement's own escalation; a child already gone makes it a no-op.
    """
    async with run.stop_lock:
        await _terminate_shielded(
            run.process,
            run.execution.ctx.cancel_grace,
            owns_group=run.execution.owns_process_group,
            observation=run.execution.observation,
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
    # A caller's wait must not depend on a consumer draining a live queue: a
    # full queue blocks the drain, the full pipe blocks the child, and the
    # exit this wait is for would never arrive. Settlement therefore stops
    # waiting for capacity before the exit wait. A tail that still fits is
    # delivered; the remainder is discarded and reported as truncation, which
    # is the documented cost of waiting before consuming to EOF.
    streams.begin_settlement()
    # Every exit path releases the output transports after reconciliation;
    # only the success path also closes the delivery worker, and only after
    # the result's figures are settled.
    hooks = _ReconcileHooks(
        after=functools.partial(
            _release_output_transports,
            run.process,
            execution,
        ),
    )
    try:
        exit_code, exited_at = await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(run.process, execution),
            run.ownership.stdin_task,
            consumers=run.ownership.consumers,
            stdin_log=run.ownership.stdin_log,
        )
    except BaseException as error:
        failure = _publish_early_failure(run.ownership, streams, error)
        await _shielded_cleanup(streams.stop.await_stop())
        try:
            await _finalize_settled_run(
                run,
                execution,
                preserve=False,
                hooks=hooks,
            )
        finally:
            streams.finalize_settlement()
        if failure is error:
            raise
        raise failure from error

    await _shielded_cleanup(streams.stop.await_stop())
    try:
        drained = await _finalize_settled_run(
            run,
            execution,
            preserve=True,
            hooks=hooks,
        )
    except BaseException:
        streams.finalize_settlement()
        raise

    late_failure = _publish_consumer_failures(run.ownership, streams)
    streams.finalize_settlement()
    if late_failure is not None:
        index, cause = late_failure
        raise LiveStreamError(_STREAM_NAMES[index], cause=cause)

    await _close_handle_delivery(execution)
    return _assemble_handle_result(
        run,
        _handle_outcome(exit_code, exited_at, drained),
        capture_bytes=True,
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
    failures = _failed_consumers(ownership)
    for index, failure in failures:
        streams.fail(index, failure)
    selected = next(
        (index for index, failure in failures if failure is error),
        None,
    )
    if selected is None:
        streams.begin_settlement()
        return error
    return LiveStreamError(_STREAM_NAMES[selected], cause=error)


def _failed_consumers(
    ownership: _RunTaskOwnership,
) -> tuple[tuple[int, BaseException], ...]:
    """Return every consumer's non-cancellation failure, in stream order.

    The reconciliation gathers consumers with ``return_exceptions=True`` so a
    failed reader cannot mask the run's own error. That also means a reader
    that failed during the final drain would otherwise be reported as an empty
    capture, so the failures are re-read here and given precedence over the
    successful result. The rendezvous helper is reused rather than restated:
    both callers ask exactly "did this task fail".

    Returns
    -------
    tuple[tuple[int, BaseException], ...]
        The stream index and failure of each failed consumer, stdout first.
    """
    failures: list[tuple[int, BaseException]] = []
    for index, task in enumerate(ownership.consumers):
        failure = _stdin_writer_failure(task)
        if failure is not None:
            failures.append((index, failure))
    return tuple(failures)


def _publish_consumer_failures(
    ownership: _RunTaskOwnership,
    streams: _ByteStreams,
) -> tuple[int, BaseException] | None:
    """Publish every failed consumer's state; return the first for the run.

    Publishing each failure before the queues are finalized is what lets a
    reader whose drain failed report ``LiveStreamError`` even when another
    stream failed first. The returned failure keeps stdout-first precedence
    for the error the shared settlement raises.

    Returns
    -------
    tuple[int, BaseException] | None
        The first failed stream and its failure, or ``None`` when no consumer
        failed.
    """
    failures = _failed_consumers(ownership)
    for index, failure in failures:
        streams.fail(index, failure)
    return failures[0] if failures else None
