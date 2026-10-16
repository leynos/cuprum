"""Waiting for subprocess exit, and reconciling its stream consumers.

Split from ``cuprum._subprocess_execution`` so the runner module is about
orchestration — spawning, wiring streams, assembling the result — while the
rules for *ending* a run live here: how the run's stream consumers are
reconciled exactly once, how a stdin writer's failure is raced against the
exit wait, and in what order the tasks that must stop before they drain
are stopped.

Those rules divide along a seam this module sits on one side of. Everything
about the *child* — how long to wait for its exit, what a deadline expiry does
to it, and the remainder that bounds what follows — is
``cuprum._subprocess_deadline``'s. The race that ends a run on a stdin
failure rather than on the child's exit is ``cuprum._subprocess_rendezvous``'s.
The drain rules a teardown runs are ``cuprum._stream_drain``'s, and the types
this module and that one exchange are ``cuprum._subprocess_wait_types``'s.
This module imports all four and re-exports them, so the splits stay an
internal arrangement: ``cuprum._subprocess_wait`` remains the one import
path for every caller and for the test suite's monkeypatch seams.

The reconciliation itself is owned by ``_reconcile_run_tasks`` so its callers
can run it under ``_shielded_cleanup`` as one unit.

Teardown routes the termination through the run's *owns_group* flag, so a
caller that omits it silently reduces an owning run to the direct-child signal
and lets the group survive. Requiring the keyword makes an omitted ownership
value a ``TypeError`` at the call site instead of a lost guarantee, and every
caller that targets the child alone must say so.
"""

from __future__ import annotations

import typing as typ

from cuprum._capture_cleanup import _finalize_capture_handles
from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _terminate_all_shielded
from cuprum._stream_drain import (
    _CAPTURE_EOF_GRACE_S,
    _await_eof_grace,
    _consumer_awaitable,
    _drain_stream_consumers,
)
from cuprum._subprocess_deadline import (
    _deadline_for_collection,
    _wait_for_exit_code,
    _wait_for_exit_code_within_timeout,
)
from cuprum._subprocess_rendezvous import _await_exit_or_writer_failure
from cuprum._subprocess_stdin_reclaim import _cancel_stdin_writer
from cuprum._subprocess_wait_types import _ConsumerPair, _DrainContext
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import (
        _DrainPair,
        _RunTaskOwnership,
    )


# ``_CAPTURE_EOF_GRACE_S``, ``_await_eof_grace``, ``_consumer_awaitable``, and
# ``_deadline_for_collection`` are imported rather than defined here, but they
# stay bound in this module's namespace deliberately: the drain and the exit
# wait once lived here, and callers still import them by this path. Bouncing
# them through keeps one definition while leaving those imports working.
#
# Importing them from here is supported; patching them here is not. Each
# resolves its own name from its own module's globals, so rebinding the copy
# in this namespace is invisible to it. Tests that need to replace the grace
# waiter must patch ``cuprum._stream_drain._await_eof_grace``, and tests that
# replace the exit wait or the collection deadline must patch
# ``cuprum._subprocess_deadline``'s names.
#
# The drain's own log records are emitted under the historical channel name
# ``"cuprum._subprocess_wait"``, spelled explicitly in ``_stream_drain``, so
# moving the code did not move the channel operators filter on.


def _drain_context(
    execution: _SubprocessExecution,
    tasks: _RunTaskOwnership,
    pid: int | None,
    *,
    preserve: bool,
) -> _DrainContext:
    """Build the drain context for one reconciliation of *tasks*.

    Every reconciliation route a streamed run has needs the same four values,
    and the only one they disagree about is whether output is preserved: the
    expiry routes keep the text their ``TimeoutExpired`` will report, while a
    route re-raising some other error discards it rather than spending the
    capture grace on text nobody will read. Building the bundle at each site
    instead is how a route drifts from the others, so the one value that
    legitimately differs is the one asked for here. A discarding route reports
    ``(None, None)``, the pair that says no stream is being captured at all.

    It sits beside :func:`_reconcile_run_tasks`, its only consumer, because the
    context exists to serve that one reconciliation and reads its cancellation
    policy from the tasks it is given.

    ``capture_bytes`` is passed through from the execution rather than
    defaulted, so an absent reader under a byte-exact run is handed ``b""``
    and never the text-mode ``""``.

    Returns
    -------
    _DrainContext
        The context that reconciliation, and the drain beneath it, read their
        capture choice, pid, observation, and cancellation policy from.

    Examples
    --------
    A discarding route reports no policies at all, which is how the drain knows
    to skip the capture grace::

        context = _drain_context(execution, tasks, pid, preserve=False)
    """
    return _DrainContext(
        captures=(
            (execution.capture_stdout, execution.capture_stderr)
            if preserve
            else (None, None)
        ),
        capture_bytes=execution.capture_bytes,
        pid=pid,
        observation=execution.observation,
        discard_on_cancel=tasks.discard_on_cancel,
    )


async def _settle_owned_group_before_draining(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Terminate the run's owned group so the drain that follows can reach EOF.

    Once the direct child has exited, the run still has a post-exit phase to
    finish. For an owning run that exit is not settlement: a descendant the
    child left behind is still in the group, still holds the write end of the
    run's pipes, and no signal aimed at the leader can reach it. A drain begun
    first would spend the bounded capture grace on readers that cannot reach EOF
    and then discard output the descendant was still producing, and a writer
    parked on the child's stdin would never be released at all.

    So the group is settled *before* whatever the caller does next, on every
    route that has something left to drain rather than only on the failure
    routes. Both run shapes need it — the streamed run before its gather, the
    direct run before its writer is reclaimed — which is why it lives here
    rather than beside either of them.

    The teardown is shielded like every other one on a failure path, so a
    cancellation arriving during its grace period cannot skip the escalation.

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


async def _reconcile_run_tasks(
    tasks: _RunTaskOwnership,
    context: _DrainContext,
) -> _DrainPair:
    """Stop the idle heartbeat, cancel the stdin writer, then drain the streams.

    The stream consumers drain with ``return_exceptions=True``, so their
    already-recorded diagnostics survive the cancellation that a teardown
    performs: a cancelled reader keeps the fallback it recorded before it was
    cancelled.

    Every half of the teardown is one unit so a caller can run them under
    :func:`_shielded_cleanup` and know all of them finish: draining first would
    leave a writer blocked on a pipe nobody is reading, and shielding them
    separately would let a cancellation landing between two of them strand the
    rest. The accumulator cleanup below belongs to that unit for the same
    reason, and it is placed here rather than inside the drain because a
    *drain-local* ``finally`` would not run when the drain task is cancelled
    mid-await — which is precisely the case a spill file has to survive.

    The heartbeat goes first. Reconciliation runs once the run is already
    ending, and a keepalive announcing that a terminated child is "still
    running" is worse than no keepalive at all. Stopping is idempotent, so the
    run's other exit paths can call it too.

    Returns
    -------
    _DrainPair
        The stdout and stderr results, as produced by
        :func:`_drain_stream_consumers`.
    """
    await _stop_idle_monitor(tasks.idle)
    await _cancel_stdin_writer(tasks.stdin_task)
    stdout_drain, stderr_drain = await _drain_stream_consumers(
        tasks.consumers,
        context,
    )
    # After the drain, not before: a reader still running is still appending,
    # and closing the file underneath it would turn the tail of a spilled
    # capture into a storage failure that the run then has to report.
    await _finalize_capture_handles((stdout_drain, stderr_drain), tasks.captures)
    for diagnostics in tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain


__all__ = [
    "_CAPTURE_EOF_GRACE_S",
    "_ConsumerPair",
    "_DrainContext",
    "_await_eof_grace",
    # Re-exported from ``cuprum._subprocess_rendezvous``, which owns the race
    # that ends a run on a stdin failure. Kept in this module's namespace so
    # the single-command run, the line-stream coordinator, and the timeout test
    # modules keep one import path. Sorted into place, not grouped, because the
    # repository's linter requires this list to stay isort-ordered.
    "_await_exit_or_writer_failure",
    # Re-exported from ``cuprum._stream_drain``, which owns the drain rules a
    # teardown runs, for the same one-import-path reason as ``_DrainContext``:
    # the drain once lived here and callers still reach for it by this path.
    # Sorted into place, not grouped, because the repository's linter requires
    # this list to stay isort-ordered.
    "_consumer_awaitable",
    "_deadline_for_collection",
    "_drain_context",
    "_drain_stream_consumers",
    "_reconcile_run_tasks",
    "_settle_owned_group_before_draining",
    "_wait_for_exit_code",
    "_wait_for_exit_code_within_timeout",
]
