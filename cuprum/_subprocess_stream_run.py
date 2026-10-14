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

from cuprum._capture_cleanup import _finalize_capture_handles
from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_stdin_stream import _stdin_codec
from cuprum._subprocess_stdin_reclaim import (
    _cancel_stdin_writer,
    _settle_stdin_writer,
)
from cuprum._subprocess_stream_exit import (
    # Re-exported under this module's name as well as imported for use: the
    # module is where the repository's commentary points for the exit wait, and
    # ``__all__`` below keeps that path open. ``_reconcile_shielded`` is *not*
    # re-imported, because it is reached only from inside the exit module and a
    # second binding here would be one no call path resolves.
    _wait_for_streamed_process_exit as _wait_for_streamed_process_exit,
)
from cuprum._subprocess_stream_post_exit import (
    # Re-imported, not moved outright: ``_await_stdin_writer`` and
    # ``_settle_owned_group_before_draining`` are still called from this
    # module's own helpers below, and those calls resolve the names from *this*
    # module's globals. Keeping them bound here is what lets the teardown
    # tests go on patching them where the production call path reads them.
    _await_stdin_writer as _await_stdin_writer,
)
from cuprum._subprocess_stream_post_exit import (
    _settle_owned_group_before_draining as _settle_owned_group_before_draining,
)
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _consumer_awaitable,
    _deadline_for_collection,
    _drain_context,
    _reconcile_run_tasks,
    _wait_for_exit_code_within_timeout,
)
from cuprum._subprocess_wait_types import (
    _DrainContext,
    _DrainPair,
    _RunTaskOwnership,
)

if typ.TYPE_CHECKING:
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_execution import _SubprocessExecution


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

    The wait is raced against that same writer — see
    :func:`~cuprum._subprocess_rendezvous._await_exit_or_writer_failure` — so a
    producer that dies at once ends the run with its own failure rather than
    being reported later as a slow child.

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
        exit_code, exited_at = await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(process, execution),
            stdin_task,
        )
    except BaseException:
        await _shielded_cleanup(_cancel_stdin_writer(stdin_task))
        raise
    # Settled rather than awaited: this path has no shared post-exit deadline
    # scope over it (that scope belongs to the streamed path), so a producer
    # parked in ``anext`` would make a bare await outlast the run's own
    # deadline — the terminator would fire, the child would be killed, and the
    # caller would go on waiting for the producer. See
    # :func:`~cuprum._subprocess_stdin_reclaim._settle_stdin_writer`.
    await _settle_stdin_writer(stdin_task)
    return exit_code, exited_at


async def _settle_group_and_reconcile_consumers(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> _DrainPair:
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
    the run's capture policies and hands the drained text on. A route re-raising some
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
    _DrainPair
        The drained stdout and stderr results, records included.
    """

    async def settle_then_reconcile() -> _DrainPair:
        """Settle the group, then reconcile every run task, as one unit."""
        await _settle_owned_group_before_draining(process, execution)
        return await _reconcile_run_tasks(tasks, context)

    return await _shielded_cleanup(settle_then_reconcile())


async def _collect_stream_output(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
) -> _DrainPair:
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
    _DrainPair
        The stdout and stderr results of the completed gather.
    """
    try:
        async with asyncio.timeout(_deadline_for_collection(execution)):
            await _stop_idle_monitor(execution.idle)
            await _await_stdin_writer(tasks)
            # The awaitables, not the tasks: a stream cuprum holds no pipe for
            # has ``None`` in its slot, and gathering that would raise instead
            # of settling the slot to nothing.
            stdout_drain, stderr_drain = await asyncio.gather(
                *(_consumer_awaitable(task) for task in tasks.consumers)
            )
    except TimeoutError as exc:
        # Capture-aware, matching the exit-wait route: a capturing run's text
        # is what the resulting ``TimeoutExpired`` reports, so discarding it
        # here would lose output the caller was explicitly promised.
        stdout_drain, stderr_drain = await _settle_group_and_reconcile_consumers(
            process,
            execution,
            tasks,
            _drain_context(execution, tasks, pid, preserve=True),
        )
        _handle_stream_timeout(
            exc,
            stdout_text=stdout_drain.payload,
            stderr_text=stderr_drain.payload,
            timeout=execution.timeout,
        )
    except BaseException:
        await _settle_group_and_reconcile_consumers(
            process,
            execution,
            tasks,
            _drain_context(execution, tasks, pid, preserve=False),
        )
        raise
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain


async def _run_subprocess_with_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
) -> tuple[
    int,
    float,
    _CapturedDrain,
    _CapturedDrain,
    tuple[_RelayDiagnostics, _RelayDiagnostics],
]:
    """Run subprocess with stream capture, timeout handling, and diagnostics.

    Returns
    -------
    Tuple of the exit code, exit timestamp, the stdout and stderr drain
    results, and the per-stream relay diagnostics collectors (stdout first,
    stderr second) settled by the run's reconciliation. Each drain result
    carries its payload — ``str`` or ``bytes`` according to the run's mode —
    together with the record describing how its policy retained it.
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
    # its single reconciliation point settles them exactly once. The helper
    # resolves both stream configs, which is what makes each stream's capture
    # accumulator reachable from that same point.
    spawn = _prepare_stream_spawn(execution, pid)
    tasks = _build_run_task_ownership(process, execution, spawn)
    exit_code, exited_at = await _wait_for_streamed_process_exit(
        process,
        execution,
        tasks,
        pid,
    )
    stdout_drain, stderr_drain = await _collect_stream_output(
        process,
        execution,
        tasks,
        pid,
    )
    # The success path's half of the cleanup contract. The teardown routes all
    # reach this through ``_reconcile_run_tasks``, and the pipeline's result
    # path calls it beside its own assembly; a run that simply reaches EOF owns
    # a file too, and without this it would return a record reporting
    # ``owned=True`` for a file nothing was ever going to remove.
    await _finalize_capture_handles(
        (stdout_drain, stderr_drain),
        tasks.captures,
    )
    return exit_code, exited_at, stdout_drain, stderr_drain, spawn.relay_diagnostics


__all__ = [
    "_collect_stream_output",
    "_run_subprocess_with_streams",
    "_run_subprocess_without_streams",
    "_settle_owned_group_before_draining",
    "_wait_for_streamed_process_exit",
]
