"""Waiting for a streamed child's exit, and reconciling when that wait fails.

Split from ``cuprum._subprocess_stream_run``, which would otherwise exceed the
repository's module-length ceiling. The seam is the one the code already drew:
this module owns the *exit wait* and the reconciliation that a failed wait
owes, while ``_subprocess_stream_run`` owns the post-exit collection that
follows it and both run loops that drive them.

The helpers here share one rule that is easy to get wrong in isolation: a
failure escaping the wait must reconcile **every** task the run owns before it
propagates, and the reconciliation is shielded so a cancellation arriving
during it cannot abandon the work. The timeout route hands the drained text to
the timeout handler; every other route settles promptly without capturing,
because the text it would drain is text nobody will read.

Reconciliation settles tasks but closes no pipes. Each of those routes also
releases the child's output transports, because a reader cancelled before
end-of-file keeps its parent-side descriptor with the transport that owns it —
and a descendant holding an inherited pipe means no end-of-file ever follows to
release it. The release runs inside the reconciliation's own shield rather than
after it: ``_shielded_cleanup`` finishes its task and then re-raises the
caller's cancellation, so a release awaited afterwards is skipped whenever a
cancellation interrupts the reconciliation, leaving exactly the descriptors the
release exists to close.

``_wait_for_streamed_process_exit`` and ``_reconcile_shielded`` are re-exported
from ``cuprum._subprocess_stream_run``, which is where the surrounding module
and the repository's commentary already refer to them.
"""

from __future__ import annotations

import contextlib
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_startup_abandon import _release_output_transports
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _drain_context,
    _is_early_failure,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _DrainPair, _RunTaskOwnership

__all__ = [
    "_reconcile_shielded",
    "_wait_for_streamed_process_exit",
]


@contextlib.asynccontextmanager
async def _releasing_output_transports(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> cabc.AsyncIterator[None]:
    """Release the child's output transports however the block ends.

    Yields
    ------
    None
        Control, with the release owed on every exit from the block.
    """
    try:
        yield
    finally:
        await _release_output_transports(process, execution)


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
    """Wait for the exit or an early task failure, reconciling when it fails.

    The stdin writer and the stream consumers run alongside the exit wait, so a
    task that fails ends the run there rather than after a child exit that may
    never come — see
    :func:`~cuprum._subprocess_rendezvous._await_exit_or_writer_failure`.
    Whatever that race raises — a deadline expiry, a producer or consumer
    failure, a cancellation — lands in the handlers below, which reconcile
    every stream task, and release the child's output transports, under one
    shield before the error propagates.

    Returns
    -------
    tuple[int, float]
        The exit code and exit timestamp, as produced by the race.

    Raises
    ------
    TimeoutError
        When this run's deadline expires, or when a stdin writer or stream
        consumer failed with a ``TimeoutError`` of its own. The second case is
        re-raised unchanged rather than translated: it is the task's own error,
        and identity is what tells the two apart.
    """

    async def reconcile_then_release(*, preserve: bool) -> _DrainPair:
        """Settle the run's tasks, then release the pipes it stopped reading.

        Returns
        -------
        _DrainPair
            The drained results, records included.
        """
        async with _releasing_output_transports(process, execution):
            return await _reconcile_shielded(execution, tasks, pid, preserve=preserve)

    try:
        return await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(process, execution),
            tasks.stdin_task,
            consumers=tasks.consumers,
            stdin_log=tasks.stdin_log,
        )
    except TimeoutError as exc:
        await _reconcile_after_timeout(
            exc,
            execution,
            tasks,
            reconcile_then_release,
        )
        # Only a monitored task's own failure returns from that helper; the
        # deadline route raises ``TimeoutExpired`` from inside it, so reaching
        # here means the caller's error is re-raised unchanged.
        raise
    except BaseException:
        # Cancellation, and any other failure escaping the wait — an OS error
        # while terminating, say — need the same reconciliation. Do not preserve
        # stream output while another error propagates, but settle every task
        # before re-raising the original failure unchanged.
        await _shielded_cleanup(reconcile_then_release(preserve=False))
        raise


async def _reconcile_after_timeout(
    error: TimeoutError,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    reconcile_then_release: cabc.Callable[..., cabc.Awaitable[_DrainPair]],
) -> None:
    """Settle the run after the exit wait raised a ``TimeoutError``.

    That error arrives two ways and only one of them is this run's deadline. A
    callback or stdin producer that failed with one is a monitored task's own
    error, re-raised by the race unchanged; ``TimeoutError`` is an ``OSError``
    subclass, so a type check alone cannot tell them apart. Identity can, and
    it is exact: the race raises the very object the task carries. That route
    returns, so the caller re-raises the error unchanged after reconciling
    without capturing output — the route every other non-timeout failure takes.

    Otherwise the deadline expired. The process has been terminated; the stdin
    writer is cancelled and the stream consumers drained exactly once, and the
    decoded output is handed to the timeout handler so it survives on the
    ``TimeoutExpired`` it raises. This never returns on that route.
    """
    if _is_early_failure(
        error,
        tasks.stdin_task,
        tasks.consumers,
        stdin_log=tasks.stdin_log,
    ):
        await _shielded_cleanup(reconcile_then_release(preserve=False))
        return
    stdout_drain, stderr_drain = await _shielded_cleanup(
        reconcile_then_release(preserve=True),
    )
    _handle_stream_timeout(
        error,
        stdout_text=stdout_drain.payload,
        stderr_text=stderr_drain.payload,
        timeout=execution.timeout,
    )
