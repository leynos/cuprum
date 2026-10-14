"""Waiting for a streamed child's exit, and reconciling when that wait fails.

Split from ``cuprum._subprocess_stream_run`` when the rebase onto ``main``
pushed that module past the 400-line ceiling. The seam is the one the code
already drew: this module owns the *exit wait* and the reconciliation that a
failed wait owes, while ``_subprocess_stream_run`` owns the post-exit
collection that follows it and both run loops that drive them.

The two helpers here share one rule that is easy to get wrong in isolation: a
failure escaping the wait must reconcile **every** task the run owns before it
propagates, and the reconciliation is shielded so a cancellation arriving
during it cannot abandon the work. The timeout route hands the drained text to
the timeout handler; every other route settles promptly without capturing,
because the text it would drain is text nobody will read.

``_wait_for_streamed_process_exit`` and ``_reconcile_shielded`` are re-exported
from ``cuprum._subprocess_stream_run``, which is where the surrounding module
and the repository's commentary already refer to them.
"""

from __future__ import annotations

import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _drain_context,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _DrainPair, _RunTaskOwnership

__all__ = [
    "_reconcile_shielded",
    "_wait_for_streamed_process_exit",
]


async def _reconcile_shielded(
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
    *,
    preserve: bool,
) -> _DrainPair:
    """Reconcile every task the run owns under one shield."""
    # The shield is what makes the reconciliation a guarantee: a caller
    # cancelling now would otherwise abandon the consumers mid-drain and leak
    # them. *preserve* is the caller's, because the routes disagree about what
    # the drained output is for — a preserving route hands it to the caller's
    # ``TimeoutExpired``, and a route re-raising some other error settles
    # promptly rather than spending the capture grace on output nobody will
    # read.
    return await _shielded_cleanup(
        _reconcile_run_tasks(
            tasks,
            _drain_context(execution, tasks, pid, preserve=preserve),
        )
    )


async def _wait_for_streamed_process_exit(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> tuple[int, float]:
    """Wait for the exit or a stdin failure, reconciling when that wait fails.

    The stdin writer runs alongside the exit wait, so a producer that fails
    ends the run there rather than after the child's own exit — see
    :func:`~cuprum._subprocess_rendezvous._await_exit_or_writer_failure`.
    Whatever that race raises — a deadline expiry, a producer failure, a
    cancellation — lands in the handlers below, which already reconcile every
    stream task before the error propagates.

    Returns
    -------
    tuple[int, float]
        The exit code and exit timestamp, as produced by the race.
    """
    try:
        return await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(process, execution),
            tasks.stdin_task,
        )
    except TimeoutError as exc:
        # The process has been terminated; cancel the stdin writer and drain the
        # stream consumers exactly once here, then hand the decoded output to the
        # timeout handler so it survives on the resulting TimeoutExpired.
        stdout_drain, stderr_drain = await _reconcile_shielded(
            execution,
            tasks,
            pid,
            preserve=True,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_drain.payload,
            stderr_text=stderr_drain.payload,
            timeout=execution.timeout,
        )
    except BaseException:
        # Cancellation, and any other failure escaping the wait — an OS error
        # while terminating, say — need the same reconciliation. Do not preserve
        # stream output while another error propagates, but settle every task
        # before re-raising the original failure unchanged.
        await _reconcile_shielded(execution, tasks, pid, preserve=False)
        raise
