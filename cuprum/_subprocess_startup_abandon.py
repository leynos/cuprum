"""Reclaim a child whose startup failed after its spawn but before hand-back.

A spawned child belongs to the run only once the setup that follows the spawn
has returned. Several steps in that setup can fail: the ``start`` event runs
synchronous observe hooks inline and re-raises their failures, the streamed run
prepares and starts its consumers, and the direct run creates its stdin writer.
A failure in any of them escapes the helper that spawned the child, so every
route that would normally wait for, drain, and reap it is never entered. This
module owns the reclaim those routes share.

The scope is deliberately the pre-hand-back window of ``run()``: the ``start``
event both run shapes share, the streamed run's consumer setup, and the direct
run's stdin writer. The other routes that spawn a child reach the same
guarantee from their own spawn helpers — ``start()`` from ``_start_handle_run``,
the line stream from ``_abandon_unstarted_run``, and the pipeline from
``_cleanup_spawned_processes`` — so their failures never arrive here. Once a
run has handed its child back as a :class:`~cuprum.RunningCommand`, cleanup
belongs to that handle (``cuprum._process_handle``) and not here. The sequences
are separate on purpose: the handle's caller decides when the child stops,
whereas an abandoned startup has no caller left to decide anything.

The sequence is fixed, and each step is given its own turn rather than
aborting the rest:

1. terminate the child and reap it, signalling its process group when the run
   owns one. Reaping is not incidental: a stop that only kills still leaves a
   child for this parent to collect.
2. cancel every task the failed setup had already started and gather them, so
   no writer or reader outlives the attempt that failed. A task that had
   already failed is reported under ``task_settle``; the cancellation this step
   itself requests is expected and is not.
3. release the pipes: close stdin, read stdout and stderr to end-of-file under
   the capture drain's own EOF grace, then close the output transports.
   Reading a stream is what lets its parent-side transport disconnect, and the
   wait is bounded because a descendant that inherited a pipe can outlive the
   group teardown — the limit ADR 021 records. When that limit expires, or a
   read fails, no end-of-file ever arrives, so the transport cannot close
   itself; closing it here is what releases the parent-side descriptor.
4. release the capture accumulators, now that no consumer can still be writing
   into them. A drain cancelled rather than settled never builds the result
   that would have carried its spill handle, so the accumulator is the last
   reference to a temporary file the library owns.

Ordinary cleanup failures are reported and then suppressed per step, so a later
step still runs and the failure owed to the caller — the hook or setup step
that raised — reaches it unchanged. Reporting is best-effort on the two
diagnostic channels this package already uses: a structured ``cuprum.reclaim``
log record and a ``teardown_error`` observe event naming the step that failed.
A reclaim that leaves a child, descriptor, or spill behind is therefore
visible to an operator without displacing the caller's error. A cancellation is
not suppressed; it propagates once the sequence has completed, which is why the
whole sequence runs inside one ``_shielded_cleanup`` rather than three
sequential ones.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._capture_cleanup import _cleanup_capture_sinks
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_all_shielded
from cuprum._reclaim_reporting import _report_reclaim_failure
from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution

# Chunk size for the discard read. Reading to EOF in bounded pieces rather than
# with a bare ``read()`` keeps a child that wrote a large backlog before it was
# stopped from being copied into memory all at once; the bytes are discarded.
_DISCARD_CHUNK_BYTES = 64 * 1024


def _record_started(
    recorded: list[asyncio.Task[typ.Any]] | None,
    task: asyncio.Task[typ.Any] | None,
) -> None:
    """Record one started task, so a later startup failure can reclaim it."""
    if recorded is not None and task is not None:
        recorded.append(task)


async def _abandon_started_process(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: cabc.Sequence[asyncio.Task[typ.Any]] = (),
    *,
    captures: cabc.Iterable[CaptureSink | None] = (),
) -> None:
    """Stop, reap, and release a child whose startup failed after the spawn.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The child the spawn returned. It is still this run's to reclaim even
        though no caller was ever handed it.
    execution : _SubprocessExecution
        The run's bundle, read for the teardown grace, the process-group
        ownership that decides what a signal targets, the observation a refused
        delivery is reported against, and the stdin plan its pipe is closed
        from.
    tasks : collections.abc.Sequence[asyncio.Task[typing.Any]]
        Tasks the failed setup had already started, in creation order. Empty
        when the failure happened before any task existed.
    captures : collections.abc.Iterable[CaptureSink | None]
        The capture accumulators the failed setup had resolved, if it got far
        enough to resolve them. Their spill files are removed after the tasks
        are settled, because a cancelled drain never builds the result that
        would otherwise have carried the handle. Empty when the run captures
        nothing or never reached the point of resolving its streams.

    Notes
    -----
    Nothing here raises for an ordinary failure of its own: the error the
    caller is already unwinding is the one it re-raises, and a teardown that
    replaced it would misreport why the run ended. A cancellation still
    propagates, but only after every step has run.
    """
    recorded = tuple(tasks)
    sinks = tuple(captures)

    async def abandon() -> None:
        """Run the whole reclaim as one sequence behind a single shield."""
        await _stop_child(process, execution)
        await _settle_recorded_tasks(process, execution, recorded)
        await _release_pipes(process, execution)
        await _release_captures(process, execution, sinks)

    await _shielded_cleanup(abandon())


async def _stop_child(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Terminate and reap the child, signalling its group when the run owns one."""
    try:
        await _terminate_all_shielded(
            (process,),
            _TeardownPolicy(
                execution.ctx.cancel_grace,
                owns_group=execution.owns_process_group,
            ),
            observations=(execution.observation,),
        )
    except Exception as error:  # ruff: ignore[blind-except] - a cleanup failure is reported and the sequence continues, so the caller's own error survives.
        _report_reclaim_failure(
            process,
            execution,
            operation="terminate",
            error=error,
        )


async def _settle_recorded_tasks(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: cabc.Sequence[asyncio.Task[typ.Any]],
) -> None:
    """Cancel and drain the tasks the failed setup had already started."""
    if not tasks:
        return
    try:
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as error:  # ruff: ignore[blind-except] - a task that cannot be settled is reported; the caller's error is already propagating.
        _report_reclaim_failure(
            process,
            execution,
            operation="task_settle",
            error=error,
        )
        return
    # A task that failed rather than merely cancelling is a startup failure the
    # caller never saw: the setup raised before it could await the task, so this
    # is the only place its error can surface. The cancellation this step itself
    # requested is expected and stays silent, which is why only an ordinary
    # ``Exception`` is reported — ``CancelledError`` is not one.
    for result in results:
        if isinstance(result, Exception):
            _report_reclaim_failure(
                process,
                execution,
                operation="task_settle",
                error=result,
            )
            return


async def _release_pipes(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Close stdin, discard stdout and stderr, and release their transports."""
    # Imported here rather than at module scope: the stdin module reaches the
    # pipeline internals, and the spawn module that imports this one sits on a
    # shorter load path than that.
    from cuprum._subprocess_stdin import _close_stdin

    stdin = process.stdin
    # A writer task that was cancelled has already closed the pipe in its own
    # ``finally``; asking again would report the second close as a pipe error.
    if stdin is not None and not stdin.is_closing():
        try:
            await _close_stdin(process, stdin, execution.observation)
        except Exception as error:  # ruff: ignore[blind-except] - the close is best-effort; its failure is reported and the output release still runs.
            _report_reclaim_failure(
                process,
                execution,
                operation="stdin_close",
                error=error,
            )
    # Reported rather than suppressed in place, so a read that fails or a grace
    # that expires still leaves the close below its chance to release both
    # descriptors.
    try:
        await _discard_outputs(process)
    except Exception as error:  # ruff: ignore[blind-except] - a failed drain is reported so the transport close below still runs.
        _report_reclaim_failure(
            process,
            execution,
            operation="output_discard",
            error=error,
        )
    await _release_output_transports(process, execution)


async def _discard_outputs(process: asyncio.subprocess.Process) -> None:
    """Discard both output streams within the capture drain's single grace.

    One window covers both readers, as it does in the drain itself
    (:func:`cuprum._stream_drain._await_eof_grace`), so two pipes kept open by
    a descendant cost one grace rather than one each — which is what the
    documented bound promises. Each read is still settled on its own:
    ``return_exceptions=True`` keeps a failure on one stream from cancelling
    the other, and a grace that expires reaches the caller as a ``TimeoutError``
    to suppress.

    A read that fails is re-raised once both have settled, because the module's
    contract is that an operator can see a failed discard: collected and
    dropped, the result would leave only the transport close behind it and the
    failure itself nowhere. The caller reports it and then releases the
    transports, so the descriptor still goes. Only an ordinary ``Exception`` is
    re-raised; a cancellation is the caller's own and keeps its own path
    outward, and a non-``Exception`` result is not a read failure to report.
    """
    streams = tuple(
        stream for stream in (process.stdout, process.stderr) if stream is not None
    )
    if not streams:
        return
    async with asyncio.timeout(_CAPTURE_EOF_GRACE_S):
        results = await asyncio.gather(
            *(_discard_to_eof(stream) for stream in streams),
            return_exceptions=True,
        )
    for result in results:
        if isinstance(result, Exception):
            raise result


async def _release_output_transports(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Close the output transports the child's owner still holds open.

    The discard above ends either at end-of-file, which closes the transport
    itself, or at the EOF grace, which means a descendant still holds the
    pipe's write end and no end-of-file is coming. The second case is why this
    exists, and it covers a read that failed for the same reason: the stream
    is never read again, so releasing the parent's descriptor discards nothing
    a caller could still have wanted.

    Which close to ask is decided through ``getattr`` rather than a type
    test, because the two backends release a descriptor differently. The
    ``wait4`` owner reaps with ``wait4`` alone and closes nothing of its own
    accord, so it is asked directly. The fallback hands back asyncio's own
    subprocess, whose read pipes are held by the transports belonging to its
    streams; those transports close a pipe only once it reports end-of-file,
    which is exactly what a descendant holding the write end prevents, so each
    is asked to close as well rather than left to collect the descriptor at an
    arbitrary later point.

    A transport releases its descriptor from the loop callback its close
    schedules, so the yield is what makes "close requested" mean "descriptor
    released" before this reclaim hands control back.
    """
    close_owned = getattr(process, "close_output_transports", None)
    if callable(close_owned):
        try:
            close_owned()
        except Exception as error:  # ruff: ignore[blind-except] - the owner's close is best-effort and its failure is reported here.
            _report_reclaim_failure(
                process,
                execution,
                operation="transport_close",
                error=error,
            )
    else:
        _close_asyncio_output_transports(process, execution)
    await asyncio.sleep(0)


def _close_asyncio_output_transports(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Ask each of the fallback's output pipes to close its parent-side end.

    asyncio publishes no accessor for the transport carrying a read pipe, so
    each stream is asked instead:
    :func:`cuprum._pipeline_stream_fds._stream_transport` already tolerates
    both spellings of that attribute and is the project's existing route to
    it. A stream answers with its own pipe transport rather than the
    subprocess transport, so closing it releases exactly that descriptor and
    leaves the child's termination and process-group ownership untouched;
    asyncio finishes the subprocess transport by itself once every pipe it
    owns has disconnected.
    """
    # Imported here rather than at module scope for the reason ``_release_pipes``
    # gives: this module sits on a shorter load path than the pipeline internals.
    from cuprum._pipeline_stream_fds import _stream_transport

    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        close = getattr(_stream_transport(stream), "close", None)
        if not callable(close):
            continue
        try:
            close()
        except Exception as error:  # ruff: ignore[blind-except] - each transport's close is best-effort; its failure is reported here.
            _report_reclaim_failure(
                process,
                execution,
                operation="transport_close",
                error=error,
            )


async def _release_captures(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    captures: cabc.Iterable[CaptureSink | None],
) -> None:
    """Remove any spill file the started consumers had opened.

    Runs after the tasks are settled: a consumer still draining into its
    accumulator could otherwise recreate the file this is removing. The
    shared cleanup gives every accumulator its own turn, so one that cannot
    remove its file does not leave the other stream's file behind; the first
    failure it reports is recorded here rather than dropped.
    """
    try:
        await _cleanup_capture_sinks(captures)
    except Exception as error:  # ruff: ignore[blind-except] - the shared cleanup reports its first failure here; the caller must not meet it.
        _report_reclaim_failure(
            process,
            execution,
            operation="capture_cleanup",
            error=error,
        )


async def _discard_to_eof(stream: asyncio.StreamReader) -> None:
    """Read a stream to end-of-file, discarding it, in bounded pieces."""
    chunk = await stream.read(_DISCARD_CHUNK_BYTES)
    while chunk:
        chunk = await stream.read(_DISCARD_CHUNK_BYTES)


__all__ = ["_abandon_started_process", "_record_started"]
