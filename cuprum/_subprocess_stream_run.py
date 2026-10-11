"""The streamed single-command run loop.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly while this module owns the streamed run:
waiting for exit through the deadline path, reconciling the stdin writer and
the stream consumers exactly once on every exit route, and handing the run's
per-stream relay diagnostics collectors back to the caller settled.

The post-exit phase has a shape that is not obvious from the code, and it is
load-bearing:

*Every* await between the child's exit and the completed gather sits inside one
boundary, because each can be cancelled and each needs the same aftermath. The
idle monitor is stopped first — the child has exited, so silence no longer means
anything. Then the stdin writer, which closes the child's stdin before the
consumers are gathered. Then the gather itself. A cancellation reaching any of
the three takes the run's full post-exit cleanup: settle the owned group, then
reconcile every task the run still owns.

The deadline has to bound that whole boundary, not just the gather, and as *one*
absolute deadline rather than a duration measured before the awaits. The direct
child exiting settles the *exit wait* and nothing else: a child that led an
owned group can exit before its deadline while a descendant it left behind holds
the run's inherited pipes, so the consumers never see end-of-file and an
unbounded gather hangs forever. The writer is exposed the same way and one await
earlier, parked in ``drain`` on a pipe a descendant inherited without reading
it. Expiry takes the ordinary error route rather than a special one, so the
cleanup that settles the group — precisely what releases the pipes the
descendant was holding — runs before the ``TimeoutError`` becomes the internal
``_SubprocessTimeoutError`` that the execution boundary converts into the
public ``TimeoutExpired``, the same exception a deadline expiring during the
exit wait produces.

Bounding those awaits is necessary but not sufficient, because a deadline can
expire and still never reach its handler *inside* the scope: cancellation
reaching an await whose ``_fut_waiter`` is a task is forwarded into that task,
and the boundary is woken only once it finishes. Each await inside the boundary
is therefore responsible for its own cancellation being both deliverable and
prompt — a task parked somewhere it will not finish holds the boundary open
past its own expiry. The writer is the one such await here: its close is
bounded in ``cuprum/_subprocess_stdin.py``, which is what makes it eventually
finish, and the shield on that await only means the run reaches the expiry
without first waiting out the close's grace. Neither substitutes for the other;
ADR 019 records the measurements and why the intuitive reading is wrong. What
the module guarantees is that the boundary is *woken* on expiry; settlement of
the tasks it owns is the expiry handler's, through the same reconciliation
every other exit route uses.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_all_shielded
from cuprum._streams import _RelayDiagnostics
from cuprum._subprocess_drain import (
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

    The wait is shielded. Awaiting the writer task makes it this await's
    ``_fut_waiter``, and ``Task.cancel()`` forwards — so without the shield the
    boundary is woken only after the writer has finished dying, including the
    close grace it now bounds. The shield returns the cancellation at once
    instead. It is a latency improvement and *not* what makes the run terminate:
    the writer's bounded close does that, and ADR 019 records the measurements
    showing a shielded await over an unbounded close still hangs, because the
    expiry handler's reconciliation awaits the writer it cancels.
    """
    if tasks.stdin_task is None:
        return
    await asyncio.shield(tasks.stdin_task)


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
    """Return the seconds left for the post-exit boundary, or ``None`` unbounded.

    The awaits this bounds are reached only once the exit wait has returned, so
    a run whose deadline was already consumed has no budget left for them. It is
    a remainder from the same ``perf_counter`` reference the exit wait was
    scheduled against, and it is measured here rather than earlier on purpose: a
    duration computed before the awaits it bounds would let them spend part of
    the budget and still hand the whole of it to what follows. The caller passes
    this straight to one ``asyncio.timeout`` covering all three awaits. Returning
    ``None`` for an unconfigured timeout keeps a run without a deadline
    unbounded here, exactly as it is everywhere else.

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

    The three awaits below form one boundary *and* share one deadline; the
    module docstring records why both matter and what each await is for. The
    deadline is read as the argument to ``asyncio.timeout`` so it is measured
    immediately before the scope it bounds, and it covers the heartbeat stop
    and the writer await as well as the gather — a writer parked on a pipe a
    descendant never reads is a hang the gather's own bound would never reach.

    Expiry and cancellation are handled apart deliberately. Only the timer's
    own expiry is a ``TimeoutError``; a cancellation still arrives as
    ``CancelledError`` and takes the re-raising route, which is what keeps a
    caller's cancellation distinguishable from the run's own deadline.

    Returns
    -------
    tuple[str | None, str | None]
        The decoded stdout and stderr text of the completed gather.
    """
    try:
        async with asyncio.timeout(_deadline_for_collection(execution)):
            await _stop_idle_monitor(execution.idle)
            await _await_stdin_writer(tasks)
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
    """Settle an owned group, then reconcile every task the run still owns.

    The leader has exited but its group may not have, so the group is settled
    before reaching for the tasks: a descendant still holding the pipes is what
    makes the gather fail, and no drain can succeed until it is gone.

    What follows is the run's *whole* reconciliation, not the consumers alone.
    A cancellation can arrive at either of the two awaits that precede the
    gather, and at the heartbeat stop the stdin writer has not been reached at
    all: settling the group releases the child's stdin, but releasing a pipe is
    not the same as having settled the task that was writing to it. A writer
    parked on a full pipe — a descendant that inherited stdin without reading
    it — would then outlive the run that spawned it, and under ``INHERIT``
    there is no group signal to release it either. ``_reconcile_run_tasks``
    stops the monitor, cancels and collects the writer, drains the consumers,
    and settles the diagnostics, so every task is accounted for on this route
    exactly as it is on the others.

    ``gather`` re-raises the first failure and leaves its sibling running, so a
    reader wedged on a pipe would outlive the run it belonged to. Reconcile it
    the way every other exit path does: the drain absorbs what it finds, which
    is right while another error is propagating — and here the consumer failure
    *is* that error. ``capture=False`` matches those paths, so this settles
    promptly rather than spending the capture grace on text nobody will read.

    Both phases run under **one** shield, not two. ``_shielded_cleanup``
    finishes the task it was given and then re-raises the caller's cancellation,
    so two sequential shielded calls are not one shielded sequence: a
    cancellation arriving during the first would exit this helper before the
    second was ever awaited, leaving the tasks unreconciled and their
    diagnostics unsettled. Composing them into a single coroutine makes the
    shield cover the pair, and the cancellation is re-raised only once both have
    finished.
    """

    async def settle_then_reconcile() -> None:
        """Settle the group, then reconcile every run task, as one unit."""
        await _settle_owned_group_before_draining(process, execution)
        await _reconcile_run_tasks(
            tasks,
            _DrainContext(
                capture=False,
                pid=pid,
                observation=execution.observation,
                discard_on_cancel=tasks.discard_on_cancel,
            ),
        )

    await _shielded_cleanup(settle_then_reconcile())


__all__ = [
    "_run_subprocess_with_streams",
    "_wait_for_streamed_process_exit",
]
