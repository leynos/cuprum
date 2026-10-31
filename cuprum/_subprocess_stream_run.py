"""The streamed run: a child whose output the parent captures or relays.

Split from ``cuprum._subprocess_execution`` so the orchestration module stays
about spawning and result assembly, and split *from* the direct run at
``cuprum._subprocess_direct_run`` because the two owe different debts. This
module's post-exit phase is a boundary: one deadline over three awaits that all
share an aftermath. The direct run has no such boundary — one await, then a
cleanup — and the shape of the code is what tells a reader which of the two they
are looking at.

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
from cuprum._stream_drain import _decode_consumer_drains
from cuprum._subprocess_startup_abandon import _abandon_started_process
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
    _consumer_awaitable,
    _deadline_for_collection,
    _drain_context,
    _reconcile_run_tasks,
)

if typ.TYPE_CHECKING:
    from cuprum._capture_types import CaptureSink
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _DrainContext,
        _DrainPair,
        _RunTaskOwnership,
    )


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
    module docstring records why both matter. Expiry and cancellation are
    handled apart for the reason given there, and the scope's ``expired()`` is
    what tells this run's deadline from a consumer's own ``TimeoutError``.

    Returns
    -------
    _DrainPair
        The stdout and stderr results of the completed gather.

    Raises
    ------
    TimeoutError
        When a consumer fails with its own ``TimeoutError``; re-raised
        unchanged after a discard reconciliation.
    """
    deadline = asyncio.timeout(_deadline_for_collection(execution))
    try:
        async with deadline:
            await _stop_idle_monitor(execution.idle)
            await _await_stdin_writer(tasks)
            # The awaitables, not the tasks: a stream cuprum holds no pipe for
            # has ``None`` in its slot, and gathering that would raise instead
            # of settling the slot to nothing. A settled ``None`` then decodes
            # to that stream's own empty capture rather than standing as an
            # absent result in a pair every caller reads the payload and record
            # off — a redirected stdout must report in stdout's position.
            stdout_drain, stderr_drain = _decode_consumer_drains(
                await asyncio.gather(
                    *(_consumer_awaitable(task) for task in tasks.consumers)
                ),
                context=_drain_context(execution, tasks, pid, preserve=True),
            )
    except TimeoutError as exc:
        if not deadline.expired():
            # Not this scope's expiry, so an ordinary failure wearing a
            # ``TimeoutError``. Reconcile without capturing, then re-raise it
            # unchanged for the same reason every other failure route does.
            await _settle_group_and_reconcile_consumers(
                process,
                execution,
                tasks,
                _drain_context(execution, tasks, pid, preserve=False),
            )
            raise
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

    # Every step from here to the hand-back runs before any exit route exists,
    # so a failure in one of them would otherwise leave the child running with
    # pipes nobody drains. The list is filled as each task is created rather
    # than after the set is built, because the step that fails is the one that
    # would have returned the set.
    recorded: list[asyncio.Task[typ.Any]] = []
    # Resolved by the step that builds the stream inputs, so the reclaim can
    # remove a spill file a started consumer opened. Empty until then: a
    # failure before the stream configs exist has no accumulator to release.
    captures: tuple[CaptureSink | None, CaptureSink | None] = (None, None)
    try:
        if execution.idle is not None:
            # Armed here, once the child is running: the catalogue checks and
            # the before hooks that preceded this spawn are the parent's work,
            # not the child's silence.
            execution.idle.launch()
        # The spawn context is a value snapshot, not an ownership hand-off: the
        # run keeps retaining the same collector tuple on _RunTaskOwnership so
        # its single reconciliation point settles them exactly once. The helper
        # resolves both stream configs, which is what makes each stream's
        # capture accumulator reachable from that same point.
        spawn = _prepare_stream_spawn(execution, pid)
        captures = spawn.spawn_context.captures
        tasks = _build_run_task_ownership(
            process,
            execution,
            spawn,
            recorded=recorded,
        )
    except BaseException:
        await _abandon_started_process(
            process,
            execution,
            recorded,
            captures=captures,
        )
        raise
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
    # ``owned=True`` for a file nothing was ever going to remove. Shielded for
    # the same reason the teardown is: the drains and sinks are half-closed
    # once this starts, and a cancellation that landed here would return a
    # result naming files the run still means to remove.
    await _shielded_cleanup(
        _finalize_capture_handles(
            (stdout_drain, stderr_drain),
            tasks.captures,
        )
    )
    return exit_code, exited_at, stdout_drain, stderr_drain, spawn.relay_diagnostics


__all__ = [
    "_collect_stream_output",
    "_run_subprocess_with_streams",
    "_settle_owned_group_before_draining",
    "_wait_for_streamed_process_exit",
]
