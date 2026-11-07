"""The direct run: a child whose output the parent neither captures nor echoes.

A direct run is the shape a caller gets whenever it asked for nothing from the
child's streams — ``RunOutputOptions(capture=False, echo=False)``, no sink, no
line hook, no idle reporting. No consumers are attached, so the only task the
run owns is its stdin writer, and the only debt it owes after the child exits is
to reconcile that one task.

That smallness is why it is a module rather than a branch of
:mod:`cuprum._subprocess_stream_run`, and the split is not merely about length.
The streamed run's post-exit phase is a *boundary*: one deadline over three
awaits that all share an aftermath. The direct run has no such boundary — it has
one await, and then a cleanup. Writing them side by side invited the reading
that the second is a degenerate case of the first, and the reading was wrong in
a way that cost the run its deadline: the direct path awaited its writer bare,
because a shape with one await does not look like it needs a boundary at all.

It does. The writer can park where nothing will release it — the child exits
leaving a descendant that inherited the run's stdin and never reads it — and an
unbounded await there hangs the run past a deadline it had already reached.
Because the cleanup that would settle the writer is what the hang prevents, the
descendant outlives the run as well. So this module carries the same debt the
streamed boundary does, stated for the one await it has:

*The configured timeout covers every await a run performs after its child
exits, not only the wait for the exit itself.*

Where the run *has* a configured deadline, the writer is deliberately awaited
under the remainder of it rather than being reclaimed through
:func:`~cuprum._subprocess_stdin_reclaim._settle_stdin_writer`. The settle
window is the right bound for a streamed run, whose consumers may still reach
end-of-file once the writer lets go; it is the wrong one here, because the
direct path has no consumers and nothing else to wait for. Reclaiming the
writer after a fixed window would report the child's success while quietly
truncating input the caller asked cuprum to deliver, and — for an owning run —
would leave the descendant holding the pipe unsignalled. Waiting out the
deadline and then reporting ``TimeoutExpired`` is the run saying what actually
happened: the input was not delivered, and the configured budget ran out.

A run with no ``timeout=`` has no remainder to wait out and no expiry to
report, so that argument does not reach it: an await bounded by ``None`` is
not bounded at all, and the run would hang for ever instead of saying
anything. There the shared settle window is the fallback, which is exactly
what ``main`` did before this path had a deadline at all — the choice above is
about which bound a *deadlined* run should use, not a licence to drop the
bound entirely when there is no deadline to take a remainder from.

``ADR021`` records why the group settlement matters here as much as the
cancellation does: a cancelled writer is reclaimed, but a descendant holding the
pipe is released only by the teardown, and under
:attr:`~cuprum.sh.ProcessGroupPolicy.INHERIT` there is no group to tear down.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._subprocess_rendezvous import _stdin_writer_failure
from cuprum._subprocess_startup_abandon import (
    _abandon_started_process,
    _record_started,
)
from cuprum._subprocess_stdin import _spawn_stdin_writer
from cuprum._subprocess_stdin_reclaim import (
    _await_task_to_completion,
    _cancel_stdin_writer,
    _settle_stdin_writer,
)
from cuprum._subprocess_stdin_stream import _stdin_codec
from cuprum._subprocess_stream_post_exit import _settle_owned_group_before_draining
from cuprum._subprocess_timeout import _handle_stream_timeout
from cuprum._subprocess_wait import (
    _await_exit_or_writer_failure,
    _deadline_for_collection,
    _wait_for_exit_code_within_timeout,
)
from cuprum._timeout_reporting import _report_timeout_expiry

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution


async def _await_writer_within_deadline(
    execution: _SubprocessExecution,
    stdin_task: asyncio.Task[None],
) -> None:
    """Await the stdin writer under whatever deadline the run has left.

    A writer can park where nothing will release it: the child exits leaving a
    descendant that inherited the run's stdin and never reads it, so the write
    blocks on a payload larger than the pipe buffer and no drain will ever
    happen. Awaiting it bare therefore hangs the run past a deadline it had
    already reached — and the cleanup that would settle the writer, and signal
    the descendant, is precisely what the hang prevents.

    The wait is shielded for the same reason the streamed path's is: awaiting
    the writer task makes it this await's ``_fut_waiter``, and ``Task.cancel()``
    forwards, so the expiry would be felt only after the writer had finished
    unwinding, which is unbounded when the writer is what is stuck. The shield
    lets the cancellation through at once, leaving the writer to the reclaim
    that follows. It is a timing fix rather than what makes the run terminate:
    the writer's own settle and the reclaim are what bound and finish it.

    The shield is `_await_task_to_completion` rather than `asyncio.shield`
    because the latter reports. A writer that absorbs its cancellation and then
    fails — which a producer wrapping caller code may do — is left with
    `shield`'s own `_log_on_exception` callback attached once the scope's
    cancellation lands, and from Python 3.14 that reports
    `... exception in shielded future` through the loop however carefully the
    writer's outcome is retrieved afterwards. `asyncio.wait` supplies the same
    separation and registers no reporter.

    Expiry and cancellation are read apart by the scope rather than by this
    frame. ``asyncio.timeout`` converts its own expiry into a ``TimeoutError``
    and leaves an external cancellation as ``CancelledError``, and it settles
    which of an overlapping pair it saw from its own cancel accounting.
    Re-deriving that from the scope's ``expired()`` state instead would be a
    second, less careful copy of the same judgement: it cannot tell its own
    timer from a caller's ``cancel()`` arriving after the timer fired, so that
    caller's cancellation would be reported as a timeout.

    A run with no ``timeout=`` has no remainder to spend and no expiry to
    report, so it takes the settle window instead. Passing ``None`` to
    ``asyncio.timeout`` does not mean "no bound here"; it means no bound
    anywhere, and the same parked writer then holds the run for ever with no
    deadline to translate. The settle window is what bounds that run in
    ``main``, and dropping it for this path would trade one hang for another:
    the deadline fix is about which bound a deadlined run gets, not about
    whether an undeadlined one is bounded at all.

    """
    # A remainder the exit wait already spent is passed through rather than
    # clamped: a non-positive delay schedules the scope's expiry for the next
    # loop iteration, which is the same "already elapsed" reading the exit wait
    # gives a non-positive timeout.
    remaining = _deadline_for_collection(execution)
    if remaining is None:
        await _settle_stdin_writer(stdin_task)
        return
    try:
        async with asyncio.timeout(remaining):
            await _await_task_to_completion(stdin_task)
    except TimeoutError as exc:
        _raise_direct_writer_timeout(execution, stdin_task, exc)


def _raise_direct_writer_timeout(
    execution: _SubprocessExecution,
    stdin_task: asyncio.Task[None],
    exc: TimeoutError,
) -> typ.NoReturn:
    """Preserve a producer failure or report the direct run's own expiry.

    Identity distinguishes the producer's exception from the scope's expiry.
    The direct path captures nothing, so the translated expiry carries no
    output; the public boundary reports the configured timeout in either mode.

    """
    if _stdin_writer_failure(stdin_task) is exc:
        raise exc
    if execution.timeout is not None:
        _report_timeout_expiry(
            execution.observation,
            pid=execution.observation.started_pid,
            configured_timeout=execution.timeout,
            mode="elapsed_deadline",
        )
    _handle_stream_timeout(
        exc,
        stdout_text=None,
        stderr_text=None,
        timeout=execution.timeout,
    )


async def _cleanup_direct_run(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    stdin_task: asyncio.Task[None] | None,
) -> None:
    """Settle the writer, then the group it was writing into, as one unit.

    Both are needed and neither substitutes for the other. Cancelling the writer
    is what reclaims a task parked on a pipe nobody reads; settling the group is
    what releases the descendant holding that pipe, and under ``INHERIT`` there
    is no group to settle and the cancellation is all there is.

    Composing them into one coroutine keeps a single shield over the pair. Two
    sequential shielded calls are not one shielded sequence: a cancellation
    arriving during the first would exit before the second was awaited, leaving
    the group the cleanup exists to settle untouched.
    """

    async def settle_writer_then_group() -> None:
        """Reclaim the writer, then settle the group it was writing into."""
        await _cancel_stdin_writer(stdin_task)
        await _settle_owned_group_before_draining(process, execution)

    await _shielded_cleanup(settle_writer_then_group())


async def _run_subprocess_without_streams(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> tuple[int, float]:
    """Run a subprocess directly, without stdout/stderr capture or echo.

    The direct path spawns no stream consumers, so the only task to reconcile is
    the stdin writer. Waiting for the child has always been bounded by the run's
    configured ``timeout``. Waiting for the *writer* was not, and that alone is
    enough to hang the run: see :func:`_await_writer_within_deadline`.

    The exit is raced against that writer — see
    :func:`~cuprum._subprocess_rendezvous._await_exit_or_writer_failure` — so a
    producer that dies at once ends the run with its own failure rather than
    being reported later as a slow child.

    Whatever escapes either wait — a deadline, a cancellation, or an unexpected
    failure — takes :func:`_cleanup_direct_run` before it propagates, so a stdin
    drain blocked on an unread pipe can neither delay timeout translation nor
    outlive the run it belongs to. An unexpected stdin-writer failure after the
    process exits normally still propagates unchanged.

    Creating the writer is the one startup step that precedes that boundary, so
    it is guarded separately: a failure there happens before any task exists to
    reconcile, and the child it would have written into has to be stopped and
    reaped by :func:`_abandon_started_process` instead.

    Returns
    -------
    tuple[int, float]
        The process exit code and the observation's monotonic exit timestamp.
    """
    recorded: list[asyncio.Task[typ.Any]] = []
    try:
        stdin_task = _spawn_stdin_writer(
            process,
            execution.stdio.stdin,
            _stdin_codec(execution.ctx),
            execution.observation,
        )
        _record_started(recorded, stdin_task)
    except BaseException:
        await _abandon_started_process(process, execution, recorded)
        raise
    try:
        exit_code, exited_at = await _await_exit_or_writer_failure(
            _wait_for_exit_code_within_timeout(process, execution),
            stdin_task,
        )
        if stdin_task is not None:
            await _await_writer_within_deadline(execution, stdin_task)
    except BaseException:
        # A failure from the exit wait, from the writer, or from the writer's
        # own deadline — and equally a caller's cancellation — all leave the run
        # holding a writer, and for an owning run a group it has not settled.
        await _cleanup_direct_run(process, execution, stdin_task)
        raise
    return exit_code, exited_at


__all__ = [
    "_await_writer_within_deadline",
    "_cleanup_direct_run",
    "_run_subprocess_without_streams",
]
