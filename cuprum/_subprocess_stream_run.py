"""The streamed single-command run loop.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly while this module owns the streamed run:
waiting for exit through the deadline path, reconciling the stdin writer and
the stream consumers exactly once on every exit route, and handing the run's
per-stream relay diagnostics collectors back to the caller settled.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _RunTaskOwnership,
    _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_execution import _SubprocessExecution


async def _wait_for_streamed_process_exit(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> tuple[int, float]:
    """Wait for exit and reconcile every stream task when that wait fails."""
    try:
        return await _wait_for_exit_code_within_timeout(
            process,
            execution,
        )
    except TimeoutError as exc:
        # The process has been terminated; cancel the stdin writer and drain the
        # stream consumers exactly once here, then hand the decoded output to the
        # timeout handler so it survives on the resulting TimeoutExpired. The
        # reconciliation is shielded because a caller cancelling now would
        # otherwise abandon the consumers mid-drain and leak them.
        stdout_text, stderr_text = await _shielded_cleanup(
            _reconcile_run_tasks(
                tasks,
                _DrainContext(
                    capture=execution.capture,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=tasks.discard_on_cancel,
                ),
            )
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except BaseException:
        # Cancellation, and any other failure escaping the wait — an OS error
        # while terminating, say — need the same reconciliation. Do not capture
        # stream text while another error propagates, but settle every task
        # before re-raising the original failure unchanged.
        await _shielded_cleanup(
            _reconcile_run_tasks(
                tasks,
                _DrainContext(
                    capture=False,
                    pid=pid,
                    observation=execution.observation,
                    discard_on_cancel=tasks.discard_on_cancel,
                ),
            )
        )
        raise


async def _discard_settled_streams(
    tasks: _RunTaskOwnership,
    execution: _SubprocessExecution,
    pid: int | None,
) -> None:
    """Drain the run's streams under a shield, discarding what they captured.

    For the failure routes where the stdin writer has already settled, so only
    the consumers need settling: they are cancelled and drained, and whatever
    they read is dropped, because another error is propagating and must stay
    the one that surfaces. Shielding is the point — a caller cancelling during
    the drain would otherwise abandon readers wedged on a pipe this run owns.

    ``_gather`` hands each failure to ``gather``'s first exception and leaves
    its sibling running, and a reader that outlived the run it belonged to is
    exactly what this route exists to prevent, so the drain is what makes the
    re-raise safe rather than merely tidy.
    """
    await _shielded_cleanup(
        _drain_stream_consumers(
            tasks.consumers,
            _DrainContext(
                capture=False,
                pid=pid,
                observation=execution.observation,
                discard_on_cancel=tasks.discard_on_cancel,
            ),
        )
    )


async def _await_stdin_writer_and_reconcile_consumers(
    tasks: _RunTaskOwnership,
    execution: _SubprocessExecution,
    pid: int | None,
) -> None:
    """Await the stdin writer, reconciling the consumers if that fails.

    An unexpected stdin-writer failure (or a cancellation landing on this
    await) must still reconcile the stdout/stderr consumers, mirroring the
    timeout and cancellation paths, so those tasks are cancelled and drained
    before the error propagates. The writer has already settled here, so only
    the consumers need draining. A run with no stdin writer returns at once.
    """
    if tasks.stdin_task is None:
        return
    try:
        await tasks.stdin_task
    except BaseException:
        await _discard_settled_streams(tasks, execution, pid)
        raise


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
) -> tuple[
    int,
    float,
    str | None,
    str | None,
    tuple[_RelayDiagnostics, _RelayDiagnostics],
]:
    """Run subprocess with stream capture, timeout handling, and diagnostics.

    Returns
    -------
    Tuple of the exit code, exit timestamp, captured stdout, captured
    stderr, and the per-stream relay diagnostics collectors (stdout
    first, stderr second) settled by the run's reconciliation.
    """
    # Imported here to avoid the orchestration module importing this one at
    # module load time (they reference each other's helpers).
    from cuprum._subprocess_streams import _build_spawn_context, _spawn_run_tasks

    if execution.idle is not None:
        # Armed here, once the child is running: the catalogue checks and the
        # before hooks that preceded this spawn are the parent's work, not the
        # child's silence.
        execution.idle.launch()
    # The spawn is a value snapshot, not an ownership hand-off: the run keeps
    # retaining the same collector tuple on _RunTaskOwnership so its single
    # reconciliation point settles them exactly once.
    spawn = _build_spawn_context(execution, pid)
    tasks = _spawn_run_tasks(process, execution, spawn)
    exit_code, exited_at = await _wait_for_streamed_process_exit(
        process,
        execution,
        tasks,
        pid,
    )
    # The child has exited, so silence no longer means anything: stop before
    # waiting on stream EOF, which a grandchild's inherited pipe can hold open
    # long after its parent is gone.
    await _stop_idle_monitor(execution.idle)
    await _await_stdin_writer_and_reconcile_consumers(tasks, execution, pid)
    try:
        stdout_text, stderr_text = await asyncio.gather(*tasks.consumers)
        for diagnostics in tasks.relay_diagnostics:
            diagnostics.settle()
    except BaseException:
        # Reconcile the sibling reader the way every other exit path does,
        # then re-raise: the drain absorbs what it finds, which is right while
        # another error is propagating — and here the consumer failure *is*
        # that error.
        await _discard_settled_streams(tasks, execution, pid)
        raise
    return exit_code, exited_at, stdout_text, stderr_text, spawn.relay_diagnostics


__all__ = [
    "_run_subprocess_with_streams",
    "_wait_for_streamed_process_exit",
]
