"""The two single-command run loops.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly. A run takes one of two shapes — the
*streamed* run with stdout/stderr consumers attached, and the *direct* run
without — and both live here because they owe the same debts: waiting for exit
through the deadline path, and reconciling the tasks the run owns exactly once
on every exit route. The direct run is a path of its own rather than a
degenerate case of the other for the reason its docstring below records.

The streamed run's post-exit phase has a shape that is not obvious from the
code, and it is load-bearing:

*Every* await between the child's exit and the completed gather sits inside one
boundary, because each can be cancelled and each needs the same aftermath. The
idle monitor is stopped first — the child has exited, so silence no longer means
anything. Then the stdin writer, which closes the child's stdin before the
consumers are gathered. Then the gather itself. A cancellation reaching any of
the three takes the run's full post-exit cleanup: settle the owned group, then
reconcile every task the run still owns.

The deadline bounds that whole boundary, not just the gather, and as *one*
absolute deadline rather than a duration measured before the awaits. The direct
child exiting settles the *exit wait* and nothing else: a child that led an
owned group can exit before its deadline while a descendant it left behind holds
the run's inherited pipes, so the consumers never see end-of-file and an
unbounded gather hangs forever. The writer is exposed the same way and one await
earlier, parked in ``drain`` on a pipe a descendant inherited without reading
it. Expiry takes the ordinary error route rather than a special one, so the
cleanup that settles the group — precisely what releases the pipes the
descendant was holding — runs before the ``TimeoutError`` becomes the public
``TimeoutExpired``.

Bounding those awaits is necessary but not sufficient: cancellation reaching an
await whose ``_fut_waiter`` is a task is forwarded into that task, so the
boundary is woken only once it finishes, and each await inside is therefore
responsible for its own cancellation being prompt. This module guarantees only
that the boundary is *woken* on expiry; settlement of the tasks it owns is the
expiry handler's, through the reconciliation every other exit route uses. ADR
021 records why the intuitive reading is wrong.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_all_shielded
from cuprum._subprocess_stdin import _cancel_stdin_writer, _spawn_stdin_writer
from cuprum._subprocess_stdin_stream import _stdin_codec
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _consumer_awaitable,
    _deadline_for_collection,
    _drain_context,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _DrainContext,
        _RunTaskOwnership,
        _StreamPayload,
        _StreamPayloadPair,
    )


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
    then discard output the descendant was still producing. The teardown is
    shielded like every other one on a failure path, so a cancellation arriving
    during its grace period cannot skip the escalation.

    A run that does not own its group has nothing to do here — its only
    teardown target is the direct child, which has already exited on every path
    reaching this helper — so the call is skipped rather than made a no-op.
    """
    if not execution.owns_process_group:
        return
    await _terminate_all_shielded(
        (process,),
        _TeardownPolicy(
            execution.ctx.cancel_grace,
            owns_group=True,
        ),
        observations=(execution.observation,),
    )


async def _reconcile_shielded(
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
    *,
    capture: bool,
) -> _StreamPayloadPair:
    """Reconcile every task the run owns under one shield."""
    # The shield is what makes the reconciliation a guarantee: a caller
    # cancelling now would otherwise abandon the consumers mid-drain and leak
    # them. *capture* is the caller's, because the routes disagree about what
    # the drained text is for — a capturing route hands it to the caller's
    # ``TimeoutExpired``, and a route re-raising some other error settles
    # promptly rather than spending the capture grace on text nobody will read.
    return await _shielded_cleanup(
        _reconcile_run_tasks(
            tasks,
            _drain_context(execution, tasks, pid, capture=capture),
        )
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
        # timeout handler so it survives on the resulting TimeoutExpired.
        stdout_text, stderr_text = await _reconcile_shielded(
            execution,
            tasks,
            pid,
            capture=execution.capture,
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
        await _reconcile_shielded(execution, tasks, pid, capture=False)
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
    cancellation. That cleanup is shielded, so a cancellation arriving while it
    runs cannot abandon it. An unexpected stdin-writer failure after the process
    exits normally propagates unchanged.

    Returns
    -------
    tuple[int, float]
        The process exit code and the observation's monotonic exit timestamp.
    """
    stdin_task = _spawn_stdin_writer(
        process,
        execution.stdio.stdin,
        _stdin_codec(execution.ctx),
        execution.observation,
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


async def _await_stdin_writer(tasks: _RunTaskOwnership) -> None:
    """Await the stdin writer, letting a failure reach the run's cleanup.

    The writer must finish before the consumers are gathered: it is what closes
    the child's stdin, and a consumer waiting on a child still blocked reading
    is a consumer waiting for a write that has not happened. A failure or
    cancellation here propagates to the caller's post-exit boundary, which owns
    the whole cleanup — group teardown included. Cleaning up here instead would
    be the narrower repair that leaves the group unsettled.

    The wait is shielded. Awaiting the writer task makes it this await's
    ``_fut_waiter``, and ``Task.cancel()`` forwards — so without the shield the
    boundary is woken only after the writer has finished dying, including the
    close grace it bounds. The shield returns the cancellation at once instead.
    It is a latency improvement and *not* what makes the run terminate: the
    writer's bounded close does that (ADR 021 records the measurements).
    """
    if tasks.stdin_task is None:
        return
    await asyncio.shield(tasks.stdin_task)


async def _settle_group_and_reconcile_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> _StreamPayloadPair:
    """Settle an owned group, then reconcile every task the run still owns.

    The leader has exited but its group may not have, so the group is settled
    first: a descendant still holding the pipes is what makes the gather fail,
    and no drain can succeed until it is gone.

    What follows is the run's *whole* reconciliation, not the consumers alone.
    A cancellation can arrive at either of the two awaits preceding the gather,
    and at the heartbeat stop the stdin writer has not been reached at all:
    settling the group releases the child's stdin, but releasing a pipe is not
    the same as having settled the task writing to it. A writer parked on a full
    pipe — a descendant that inherited stdin without reading it — would then
    outlive the run that spawned it, and under ``INHERIT`` no group signal
    releases it either. ``_reconcile_run_tasks`` accounts for every task on this
    route exactly as it does on the others.

    ``gather`` re-raises the first failure and leaves its sibling running, so a
    reader wedged on a pipe would outlive the run it belonged to. Reconcile it as
    every other exit path does: the drain absorbs what it finds, which is right
    while another error is propagating — and here the consumer failure *is* that
    error.

    *context* is the caller's, built by :func:`_drain_context`, because the two
    routes disagree about what the drained text is for. The deadline's expiry
    becomes a ``TimeoutExpired`` the caller receives, so a capturing run asks for
    ``execution.capture`` and hands the drained text on. A route re-raising some
    other error asks for ``False``, settling promptly rather than spending the
    capture grace on text nobody will read.

    Both phases run under **one** shield, not two. ``_shielded_cleanup``
    finishes its task and then re-raises the caller's cancellation, so two
    sequential shielded calls are not one shielded sequence: a cancellation
    during the first would exit this helper before the second was awaited,
    leaving tasks unreconciled. Composing them into one coroutine makes the
    shield cover the pair.

    Returns
    -------
    _StreamPayloadPair
        The drained stdout and stderr payloads.
    """

    async def settle_then_reconcile() -> _StreamPayloadPair:
        """Settle the group, then reconcile every run task, as one unit."""
        await _settle_owned_group_before_draining(process, execution)
        return await _reconcile_run_tasks(tasks, context)

    return await _shielded_cleanup(settle_then_reconcile())


async def _collect_stream_output(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> _StreamPayloadPair:
    """Collect the stream consumers, settling the run's group if that fails.

    The three awaits below form one boundary *and* share one deadline; the
    module docstring records why both matter. The deadline is read as the
    argument to ``asyncio.timeout`` so it is measured immediately before the
    scope it bounds, and it covers the heartbeat stop and the writer await as
    well as the gather — a writer parked on a pipe a descendant never reads is
    a hang the gather's own bound would never reach.

    Expiry and cancellation are handled apart deliberately. Only the timer's
    own expiry is a ``TimeoutError``; a cancellation still arrives as
    ``CancelledError`` and takes the re-raising route, which keeps a caller's
    cancellation distinguishable from the run's own deadline.

    Returns
    -------
    _StreamPayloadPair
        The stdout and stderr payloads of the completed gather.
    """
    try:
        async with asyncio.timeout(_deadline_for_collection(execution)):
            await _stop_idle_monitor(execution.idle)
            await _await_stdin_writer(tasks)
            stdout_text, stderr_text = await asyncio.gather(
                *(_consumer_awaitable(task) for task in tasks.consumers)
            )
    except TimeoutError as exc:
        # Capture-aware, matching the exit-wait route: a capturing run's text
        # is what the resulting ``TimeoutExpired`` reports, so discarding it
        # here would lose output the caller was explicitly promised.
        stdout_text, stderr_text = await _settle_group_and_reconcile_consumers(
            process,
            execution,
            tasks,
            _drain_context(execution, tasks, pid, capture=execution.capture),
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_text,
            stderr_text=stderr_text,
            timeout=execution.timeout,
        )
    except BaseException:
        await _settle_group_and_reconcile_consumers(
            process,
            execution,
            tasks,
            _drain_context(execution, tasks, pid, capture=False),
        )
        raise
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_text, stderr_text


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
    Tuple of the exit code, exit timestamp, captured stdout, captured stderr,
    and the per-stream relay diagnostics collectors (stdout first, stderr
    second) settled by the run's reconciliation. The payloads are ``str`` or
    ``bytes`` according to the run's mode.
    """
    # Imported here to avoid the orchestration module importing this one at
    # module load time (they reference each other's helpers).
    from cuprum._subprocess_streams import (
        _build_run_task_ownership,
        _prepare_stream_spawn,
    )

    if execution.idle is not None:
        # Armed here, once the child is running: the catalogue checks and the
        # before hooks that preceded this spawn are the parent's work, not the
        # child's silence.
        execution.idle.launch()
    # The spawn context is a value snapshot, not an ownership hand-off: the
    # run keeps retaining the same collector tuple on _RunTaskOwnership so
    # its single reconciliation point settles them exactly once.
    spawn = _prepare_stream_spawn(execution, pid)
    tasks = _build_run_task_ownership(process, execution, spawn)
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
    return exit_code, exited_at, stdout_text, stderr_text, spawn.relay_diagnostics


__all__ = [
    "_collect_stream_output",
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_settle_owned_group_before_draining",
    "_wait_for_streamed_process_exit",
]
