"""The streamed single-command run loop.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly while this module owns the streamed run:
waiting for exit through the deadline path, reconciling the stdin writer and
the stream consumers exactly once on every exit route, and handing the run's
per-stream relay diagnostics collectors back to the caller settled.

The post-exit phase has a shape that is not obvious from the code, and it is
load-bearing:

*Every* await between the child's exit and the completed gather has to sit
inside one boundary, because each of them can be cancelled and each of them
needs the same aftermath. The idle monitor is stopped first — the child has
exited, so silence no longer means anything. Then the stdin writer, which
closes the child's stdin before the consumers are gathered. Then the gather
itself. A cancellation reaching any of the three takes the run's full post-exit
cleanup: settle the owned group, then reconcile the consumers.

The gather is also the one post-exit await a deadline has to bound. The direct
child exiting settles the *exit wait* and nothing else: a child that led an
owned group can exit before its deadline while a descendant it left behind
holds the run's inherited pipes, so the consumers never see end-of-file and an
unbounded gather hangs forever — the configured timeout silently covering only
the first half of the run. Bounding the gather by the remainder of the same
deadline makes the timeout mean what it says: measured from the run's start,
and expiring whether the child or a descendant was what kept it alive. Expiry
takes the ordinary error route rather than a special one, so the cleanup that
settles the group — precisely what releases the pipes the descendant was
holding — runs before the ``TimeoutError`` becomes the internal
``_SubprocessTimeoutError`` that the execution boundary converts into the
public ``TimeoutExpired``, the same exception a deadline expiring during the
exit wait produces.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_all_shielded
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_drain import (
    _drain_stream_consumers,
    _DrainContext,
    _reconcile_run_tasks,
    _RunTaskOwnership,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _wait_for_exit_code_within_timeout,
)
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution


async def _settle_owned_group_before_draining(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Terminate the run's owned group so the drain that follows can reach EOF.

    Once the direct child has exited, this module still has stream consumers to
    collect. For an owning run that exit is not settlement: a descendant the
    child left behind is still in the group, still holds the write end of the
    run's pipes, and no signal aimed at the leader can reach it. Draining first
    would spend the bounded capture grace on readers that cannot reach EOF and
    then discard output the descendant was still producing.

    Signalling the group first is what lets those readers see EOF. The teardown
    is shielded like every other one on a failure path, so a cancellation
    arriving during its grace period cannot skip the escalation.

    A run that does not own its group has nothing to do here — its only
    teardown target is the direct child, which has already exited on every path
    that reaches this helper — so the call is skipped rather than made and
    proven a no-op.
    """
    if not execution.owns_process_group:
        return
    await _terminate_all_shielded(
        (process,),
        _TeardownPolicy(
            execution.ctx.cancel_grace,
            owns_group=True,
        ),
    )


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


async def _await_stdin_writer(tasks: _RunTaskOwnership) -> None:
    """Await the stdin writer, letting a failure reach the run's cleanup.

    The writer must finish before the consumers are gathered: it is what closes
    the child's stdin, and a consumer waiting on a child that is still blocked
    reading is a consumer waiting for a write that has not happened. A failure
    or cancellation here propagates to the caller's post-exit boundary, which
    owns the whole cleanup — group teardown included. Cleaning up here instead
    would be the narrower repair that leaves the group unsettled.
    """
    if tasks.stdin_task is None:
        return
    await tasks.stdin_task


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
    stdout_text, stderr_text = await _collect_stream_output(
        process,
        execution,
        tasks,
        pid,
    )
    return exit_code, exited_at, stdout_text, stderr_text, relay_diagnostics


def _deadline_for_collection(execution: _SubprocessExecution) -> float | None:
    """Return the seconds left for collecting the streams, or ``None`` unbounded.

    The consumers are reached only once the exit wait has returned, so a run
    whose deadline has already been consumed has no budget left for them: the
    remainder is measured from the same ``perf_counter`` reference the exit
    wait's deadline was scheduled against, and a remainder at or below zero
    means the run is already past its deadline. Returning ``None`` for an
    unconfigured timeout is what keeps a run without a deadline unbounded here,
    exactly as it is everywhere else.

    Returns
    -------
    float | None
        The seconds remaining before the run's deadline, possibly negative if
        the exit wait overran it, or ``None`` when no timeout is configured.
    """
    if execution.timeout is None:
        return None
    elapsed = time.perf_counter() - execution.started_at
    return execution.timeout - elapsed


async def _collect_stream_output(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> tuple[str | None, str | None]:
    """Collect the stream consumers, settling the run's group if that fails.

    The three awaits below form one boundary; the module docstring records why
    that matters and what each of them is for.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr text of the completed gather.
    """
    deadline = _deadline_for_collection(execution)
    try:
        await _stop_idle_monitor(execution.idle)
        await _await_stdin_writer(tasks)
        async with asyncio.timeout(deadline):
            stdout_text, stderr_text = await asyncio.gather(*tasks.consumers)
    except TimeoutError as exc:
        await _settle_group_and_discard_consumers(
            process,
            execution,
            tasks,
            pid,
        )
        _handle_stream_timeout(
            exc,
            stdout_text=None,
            stderr_text=None,
            timeout=execution.timeout,
        )
    except BaseException:
        await _settle_group_and_discard_consumers(
            process,
            execution,
            tasks,
            pid,
        )
        raise
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_text, stderr_text


async def _settle_group_and_discard_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> None:
    """Settle an owned group, then reconcile the consumers that were gathering.

    The leader has exited but its group may not have, so the group is settled
    before reaching for the consumers: a descendant still holding the pipes is
    what makes the gather fail, and no drain can succeed until it is gone.

    ``gather`` re-raises the first failure and leaves its sibling running, so a
    reader wedged on a pipe would outlive the run it belonged to. Reconcile it
    the way every other exit path does: the drain absorbs what it finds, which
    is right while another error is propagating — and here the consumer failure
    *is* that error.

    Both phases run under **one** shield, not two. ``_shielded_cleanup``
    finishes the task it was given and then re-raises the caller's cancellation,
    so two sequential shielded calls are not one shielded sequence: a
    cancellation arriving during the first would exit this helper before the
    second was ever awaited, leaving the consumers unreconciled and their
    diagnostics unsettled. Composing them into a single coroutine makes the
    shield cover the pair, and the cancellation is re-raised only once both have
    finished.
    """

    async def settle_then_drain() -> None:
        """Settle the group, then reconcile the consumers, as one unit."""
        await _settle_owned_group_before_draining(process, execution)
        await _drain_stream_consumers(
            tasks.consumers,
            _DrainContext(
                capture=False,
                pid=pid,
                observation=execution.observation,
                discard_on_cancel=tasks.discard_on_cancel,
            ),
        )
        for diagnostics in tasks.relay_diagnostics:
            diagnostics.settle()

    await _shielded_cleanup(settle_then_drain())


__all__ = [
    "_run_subprocess_with_streams",
    "_wait_for_streamed_process_exit",
]
