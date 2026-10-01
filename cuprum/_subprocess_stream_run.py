"""The two single-command run loops.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly. A run takes one of two shapes, and both
live here because they owe the same debts: waiting for exit through the
deadline path, and reconciling the tasks the run owns exactly once on every
exit route.

- The *streamed* run has stdout/stderr consumers attached and hands its
  per-stream relay diagnostics collectors back to the caller settled.
- The *direct* run attaches none, so the only task it owns is the stdin writer
  — which is exactly why it needs its own path rather than a degenerate case of
  the other: a run with no consumers still has a writer to cancel before an
  error propagates, and a stdin drain blocked on an unread pipe would otherwise
  delay timeout translation.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._stream_drain import _drain_stream_consumers
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_stdin import _cancel_stdin_writer, _spawn_stdin_writer
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum._subprocess_wait_types import _DrainContext, _RunTaskOwnership

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _StreamPayload


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
                    capture_bytes=execution.capture_bytes,
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


async def _run_subprocess_without_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Run a subprocess directly, without stdout/stderr capture or echo.

    The direct path spawns no stream consumers, so the only task to reconcile is
    the stdin writer. Whatever escapes the wait — a timeout, a cancellation, or
    an unexpected failure — it is cancelled and drained through
    :func:`_cancel_stdin_writer` *before* the exception propagates, so a stdin
    drain blocked on an unread pipe cannot delay timeout translation or
    cancellation, and no writer is left running behind a failure. That cleanup
    is shielded, so a cancellation arriving while it runs cannot abandon it. An
    unexpected stdin-writer failure after the process exits normally propagates
    unchanged.

    Returns
    -------
    tuple[int, float]
        The process exit code and the ``perf_counter`` timestamp of exit.
    """
    stdin_task = _spawn_stdin_writer(
        process, execution.stdin_data, execution.observation
    )
    try:
        exit_code, exited_at = await _wait_for_exit_code_within_timeout(
            process,
            execution,
        )
    except BaseException:
        await _shielded_cleanup(_cancel_stdin_writer(stdin_task))
        raise
    if stdin_task is not None:
        await stdin_task
    return exit_code, exited_at


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
        raise


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
) -> tuple[
    int,
    float,
    _StreamPayload | None,
    _StreamPayload | None,
    tuple[_RelayDiagnostics, _RelayDiagnostics],
]:
    """Run subprocess with stream capture, timeout handling, and diagnostics.

    Returns
    -------
    Tuple of the exit code, exit timestamp, captured stdout, captured
    stderr, and the per-stream relay diagnostics collectors (stdout
    first, stderr second) settled by the run's reconciliation. The two
    captured payloads are ``str`` or ``bytes`` according to the run's mode,
    read unchanged off the consumers.
    """
    # Imported here to avoid the orchestration module importing this one at
    # module load time (they reference each other's helpers).
    from cuprum._subprocess_streams import (
        _build_stream_config,
        _spawn_stream_consumers,
        _StreamConsumerSpawnContext,
    )

    if execution.idle is not None:
        # Armed here, once the child is running: the catalogue checks and the
        # before hooks that preceded this spawn are the parent's work, not the
        # child's silence.
        execution.idle.launch()
    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    # The spawn context is a value snapshot, not an ownership hand-off: the
    # run keeps retaining the same collector tuple on _RunTaskOwnership so
    # its single reconciliation point settles them exactly once.
    spawn_context = _StreamConsumerSpawnContext(
        stream_config=stream_config,
        pid=pid,
        relay_diagnostics=relay_diagnostics,
    )
    tasks = _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process, execution.stdin_data, execution.observation
        ),
        consumers=_spawn_stream_consumers(
            process,
            execution,
            spawn_context,
        ),
        discard_on_cancel=discard_on_cancel,
        relay_diagnostics=relay_diagnostics,
        idle=execution.idle,
    )
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
        # `gather` re-raises the first failure and leaves its sibling running,
        # so a reader wedged on a pipe would outlive the run it belonged to.
        # Reconcile it the way every other exit path does, then re-raise: the
        # drain absorbs what it finds, which is right while another error is
        # propagating — and here the consumer failure *is* that error.
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
        raise
    return exit_code, exited_at, stdout_text, stderr_text, relay_diagnostics


__all__ = [
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_wait_for_streamed_process_exit",
]
