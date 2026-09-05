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

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _RunTaskOwnership,
    _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
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
    from cuprum._subprocess_execution import (
        _build_stream_config,
        _spawn_stream_consumers,
    )

    discard_on_cancel = asyncio.Event()
    stream_config = _build_stream_config(execution, discard_on_cancel)
    relay_diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    tasks = _RunTaskOwnership(
        stdin_task=_spawn_stdin_writer(
            process, execution.stdin_data, execution.observation
        ),
        consumers=_spawn_stream_consumers(
            process,
            execution,
            stream_config,
            pid=pid,
            relay_diagnostics=relay_diagnostics,
        ),
        discard_on_cancel=discard_on_cancel,
        relay_diagnostics=relay_diagnostics,
    )
    exit_code, exited_at = await _wait_for_streamed_process_exit(
        process,
        execution,
        tasks,
        pid,
    )
    if tasks.stdin_task is not None:
        try:
            await tasks.stdin_task
        except BaseException:
            # An unexpected stdin-writer failure (or a cancellation landing on
            # this await) must still reconcile the stdout/stderr consumers,
            # mirroring the timeout and cancellation paths above, so those tasks
            # are cancelled and drained before the error propagates. The writer
            # has already settled here, so only the consumers need draining.
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
    "_wait_for_streamed_process_exit",
]
