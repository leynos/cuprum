"""Line-stream early stops must release the child's output transports.

``SafeCmd.lines()`` can end before its streams have necessarily drained: an
``on_line`` callback can fail, the run's own deadline can expire, the caller
can close the iterator mid-flight, the post-exit collect can expire, and a
post-spawn ``start`` hook can fail. Each route settles the readers — cancels
and gathers them — but settling is not closing: a reader cancelled before
end-of-file keeps its parent-side descriptor with the transport that owns it,
and a descendant holding the inherited write ends means no end-of-file ever
arrives to release it.

Every case drives a real child on both spawn backends and asks the descriptors
themselves whether they were released. The ``wait4`` owner reaps without
closing anything, and the fallback's asyncio transports close only at
end-of-file, which the descendant prevents: only the run's own release can
close these.

The pre-exit cases use a holder that announces itself *after* its
separate-session descendant has recorded its pid, so the line a case acts on
can never arrive before the descendant holds the pipes. Without that ordering
a failing callback or an early close could race the fork, terminate a child
whose pipes were not yet inherited, and pass without exercising a release.

The post-exit cases use ``descendant_then_exit_argv`` under ``INHERIT``: the
child exits, so the exit wait settles, and the descendant it left behind holds
the write ends, so the consumers never reach end-of-file. That is the route the
run's own post-exit collection has to release.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import sh
from cuprum._line_stream import spawn as line_stream_spawn
from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S
from cuprum._subprocess_stdin_reclaim import _STDIN_RECLAIM_BUDGET_S
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
    TimeoutExpired,
)
from cuprum.unittests._post_exit_teardown_support import (
    RUN_BUDGET_S,
    Gate,
    owned_builder,
    reach_gate_and_cancel,
    reap_descendant,
    run_to_its_own_deadline,
)
from cuprum.unittests._rendezvous_support import record_rendezvous
from cuprum.unittests._startup_abandon_support import _posix_only
from cuprum.unittests._transport_release_support import (
    _ANNOUNCING_HOLDER_SCRIPT,
    _DESCENDANT_LIFETIME_S,
    _WATCHDOG_S,
    BackendTraversal,
    assert_released_before_the_descendant_ends,
    await_recorded_pid,
    record_spawn,
    spawn_backend,
)
from tests.helpers.execution import _RunKwargs
from tests.helpers.timeouts import descendant_then_exit_argv, pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._line_iteration import LineStream
    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent, LineHook

# The fixture is imported rather than defined here; declaring it records that
# pytest resolves the name at collection time rather than leaving it to be
# pruned as an unused import.
__all__ = ["owned_builder", "spawn_backend"]

pytestmark = _posix_only

#: Long enough for the holder to fork and announce itself, short enough that a
#: case which waited for the child could not finish inside its own watchdog.
_DEADLINE_S = 1.0

#: Printed by the post-exit child before it spawns its descendant, so a case
#: can assert the text already delivered survives the expiry.
_MARKER = "before-the-descendant"

#: The line the pre-exit holder writes once its descendant holds the pipes.
_ANNOUNCED_LINE = "holding"


class _CallbackError(RuntimeError):
    """The failure an ``on_line`` callback ends the run on."""


class _StartHookError(RuntimeError):
    """The failure a synchronous ``start`` observe hook raises."""


def _lines_kwargs(
    *,
    timeout: float | None = None,
    on_line: LineHook | None = None,
    idle_after: float | None = None,
) -> _RunKwargs:
    """Build the keyword arguments every case drives ``lines()`` with.

    ``INHERIT`` is deliberate: it leaves the run with no group signal that
    could close the pipes, so only the run's own release can close them. A
    case that relied on a group teardown would pass on an owned group whether
    or not the release ever ran.

    Returns
    -------
    _RunKwargs
        The captured, inherited-group invocation this module needs.
    """
    return _RunKwargs(
        output=RunOutputOptions(
            capture=True, echo=False, on_line=on_line, idle_after=idle_after
        ),
        timeout=timeout,
        context=ExecutionContext(process_group=ProcessGroupPolicy.INHERIT),
    )


def _holder_command(
    builder: cabc.Callable[..., SafeCmd],
    pid_file: pathlib.Path,
) -> SafeCmd:
    """Build a child that leaves a separate-session descendant on its pipes."""
    return builder(
        "-c",
        _ANNOUNCING_HOLDER_SCRIPT,
        str(pid_file),
        str(_DESCENDANT_LIFETIME_S),
    )


def _failing_callback(sentinel: BaseException) -> LineHook:
    """Return an ``on_line`` callback that fails the run with *sentinel*.

    The received text is asserted before the raise. The holder writes its line
    only after its separate-session descendant has recorded itself, so a
    delivered line is itself the proof that the descendant held the pipes when
    the callback failed; a case that failed earlier would prove nothing about
    the release.

    Returns
    -------
    LineHook
        A synchronous callback that raises *sentinel* on the announcement.
    """

    def fail(event: LineEvent) -> None:
        """Fail the run once the holder's announced line arrives."""
        assert event.text == _ANNOUNCED_LINE, (
            "the callback must fail on the holder's announcement, which follows "
            f"the fork that hands over the pipes; got {event.text!r}"
        )
        raise sentinel

    return fail


async def _consume_lines(command: SafeCmd, kwargs: _RunKwargs) -> None:
    """Iterate a line stream to whatever ends it."""
    async for _event in command.lines(**kwargs):
        pass


async def _close_after_the_first_line(command: SafeCmd, kwargs: _RunKwargs) -> None:
    """Take one line, then close the stream while the child is still running."""
    stream = command.lines(**kwargs)
    try:
        async for _event in stream:
            break
    finally:
        await stream.aclose()


async def _drain_lines(lines: cabc.AsyncIterator[LineEvent], seen: list[str]) -> None:
    """Consume *lines* to completion, appending each event's text to *seen*.

    The caller supplies the list rather than receiving one because the reader
    does not always finish normally: an expiry or a cancellation ends the task,
    and the lines already delivered are evidence that has to outlive it.
    """
    async for event in lines:
        seen.append(event.text)  # ruff: ignore[manual-list-comprehension] - a comprehension would lose the partial result the expiry cases assert on


def _failing_start_hook(
    sentinel: BaseException,
    pid_file: pathlib.Path,
) -> cabc.Callable[[ExecEvent], None]:
    """Return a synchronous hook that fails once the child has started.

    It waits for the holder to record its descendant before raising. ``start``
    is emitted immediately after the spawn, so a hook that failed at once would
    reach the reclaim before the child had forked, and the case would terminate
    a child whose pipes were never inherited.

    Returns
    -------
    collections.abc.Callable[[ExecEvent], None]
        The hook, which raises *sentinel* at ``start``.
    """

    def hook(event: ExecEvent) -> None:
        """Fail the ``start`` announcement, which follows the spawn."""
        if event.phase == "start":
            assert await_recorded_pid(pid_file), (
                "the holder never recorded its descendant, so the child had "
                "not reached the fork this case depends on"
            )
            raise sentinel

    return hook


def test_an_on_line_failure_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing callback ends the run before its streams drained.

    The child's descendant keeps both write ends open, so neither pipe reaches
    end-of-file and neither transport closes itself. The caller must still meet
    the callback's own exception, the child must be reaped, and both
    parent-side descriptors must be closed while the descendant is alive.
    """
    context = f"an on_line failure on {spawn_backend.requested}"
    pid_file = tmp_path / "callback.pid"
    command = _holder_command(owned_builder, pid_file)
    sentinel = _CallbackError("the callback failed")
    kwargs = _lines_kwargs(on_line=_failing_callback(sentinel))
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")

    async def run_case() -> None:
        """Drive the failure and read the descriptors it left open."""
        descendant_pid: int | None = None
        try:
            with pytest.raises(_CallbackError) as caught:
                await asyncio.wait_for(_consume_lines(command, kwargs), _WATCHDOG_S)
            assert caught.value is sentinel, (
                "the callback's own exception must reach the caller, got "
                f"{caught.value!r}"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
            assert not pending_tasks(), (
                f"{context} left tasks pending on the loop, so the run stopped "
                "without reconciling what it stopped waiting for"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_configured_deadline_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deadline route releases the pipes it stopped reading.

    The route preserves whatever the readers captured for the ``TimeoutExpired``
    it raises, and that drain is where a close could be forgotten: the output
    has been taken, so nothing further reads the pipe.
    """
    context = f"a deadline expiry on {spawn_backend.requested}"
    pid_file = tmp_path / "deadline.pid"
    command = _holder_command(owned_builder, pid_file)
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")

    async def run_case() -> None:
        """Expire the run's own deadline, then read its descriptors."""
        descendant_pid: int | None = None
        try:
            with pytest.raises(TimeoutExpired) as caught:
                await asyncio.wait_for(
                    _consume_lines(command, _lines_kwargs(timeout=_DEADLINE_S)),
                    RUN_BUDGET_S,
                )
            assert caught.value.timeout == _DEADLINE_S, (
                "the expiry must report the timeout the caller configured, got "
                f"{caught.value.timeout!r}"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_closing_the_iterator_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A caller's early close is a cancellation, and it abandons the pipes.

    Closing mid-iteration is the ordinary way a line-reading caller stops, and
    the route least likely to reach any later cleanup, because control returns
    to the caller with the child already terminated.
    """
    context = f"an early close on {spawn_backend.requested}"
    pid_file = tmp_path / "close.pid"
    command = _holder_command(owned_builder, pid_file)
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")

    async def run_case() -> None:
        """Close the stream after one line, then read the descriptors."""
        descendant_pid: int | None = None
        try:
            await asyncio.wait_for(
                _close_after_the_first_line(command, _lines_kwargs()),
                _WATCHDOG_S,
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
            assert not pending_tasks(), (
                f"{context} left tasks pending on the loop, so the coordinator "
                "was abandoned rather than reconciled"
            )
        finally:
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_post_exit_deadline_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-exit collect releases the pipes its expiry stopped reading.

    The child exits successfully, so the exit wait settles and the run reaches
    its own post-exit collection. The descendant it leaves behind holds the
    write ends, so the consumers never see end-of-file and the collection
    expires. The text the child delivered before it forked must survive on the
    ``TimeoutExpired``, and both descriptors must be closed while the
    descendant lives.
    """
    context = f"a post-exit expiry on {spawn_backend.requested}"
    pid_file = tmp_path / "post-exit.pid"
    command = owned_builder(
        *descendant_then_exit_argv(
            pid_file,
            exit_code=0,
            stdout_chunks=(f"{_MARKER}\n",),
        )
    )
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")
    handoff = record_rendezvous(
        monkeypatch, "cuprum._line_stream.coordinator._await_exit_or_writer_failure"
    )

    async def run_case() -> None:
        """Run to the post-exit deadline and read what the run left behind."""
        descendant_pid: int | None = None
        delivered: list[str] = []
        lines = command.lines(**_lines_kwargs(timeout=_DEADLINE_S))
        reader = asyncio.ensure_future(_drain_lines(lines, delivered))
        try:
            expired = await run_to_its_own_deadline(
                reader,
                cleanup_allowance_s=_STDIN_RECLAIM_BUDGET_S + _CAPTURE_EOF_GRACE_S,
            )
            assert handoff.is_set(), (
                "the deadline must expire in the post-exit collection, but the "
                "exit rendezvous never settled, so the expiry was reached one "
                "phase earlier and this case proves nothing about it"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
            assert _MARKER in "".join(delivered), (
                "the lines the child delivered before the deadline must survive "
                f"the expiry and reach the caller, got {delivered!r}"
            )
            # Checked for text before membership: the payload is widened for
            # bytes-mode runs, and `str in bytes` is not a meaningful question.
            expired_text = expired.stdout
            assert isinstance(expired_text, str), (
                "a text-mode line stream must report its captured text as str, "
                f"got {type(expired_text).__name__}"
            )
            assert _MARKER in expired_text, (
                "the expiry must carry the output the run had already captured, "
                f"got {expired_text!r}"
            )
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_post_exit_cancellation_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation after the exit settled must release the abandoned pipes.

    The gate is the real rendezvous returning, not the child's ``returncode``:
    a cancellation issued before the race settled would prove something about
    the exit wait rather than about the collection this case names.
    """
    context = f"a post-exit cancellation on {spawn_backend.requested}"
    pid_file = tmp_path / "post-exit-cancel.pid"
    command = owned_builder(
        *descendant_then_exit_argv(
            pid_file,
            exit_code=0,
            stdout_chunks=(f"{_MARKER}\n",),
        )
    )
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")
    handoff = record_rendezvous(
        monkeypatch, "cuprum._line_stream.coordinator._await_exit_or_writer_failure"
    )

    async def run_case() -> None:
        """Cancel once the exit has settled, then read the descriptors."""
        descendant_pid: int | None = None
        delivered: list[str] = []
        reader = asyncio.ensure_future(
            _drain_lines(command.lines(**_lines_kwargs()), delivered)
        )
        try:
            await asyncio.wait_for(handoff.wait(), _WATCHDOG_S)
            reader.cancel()
            outcome = await asyncio.gather(reader, return_exceptions=True)
            assert isinstance(outcome[0], asyncio.CancelledError), (
                "the reader must end by the cancellation this case issued, got "
                f"{outcome[0]!r}"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_failed_start_hook_releases_both_descriptors(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup abandonment of a fully built run releases its pipes.

    ``start`` is emitted after every task exists, so a raising hook reaches the
    reclaim with a whole run to unwind. The caller meets the hook's own error
    only once the child has been stopped and reaped and the pipes released.
    """
    context = f"a failed start hook on {spawn_backend.requested}"
    pid_file = tmp_path / "start-hook.pid"
    command = _holder_command(owned_builder, pid_file)
    sentinel = _StartHookError("the start hook failed")
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")

    async def run_case() -> None:
        """Start under a failing hook, then read the descriptors it left."""
        descendant_pid: int | None = None
        stream: LineStream | None = None
        try:
            # Built inside the observer so the stream cannot capture the
            # ambient hooks before the failing one is installed.
            with sh.observe(_failing_start_hook(sentinel, pid_file)):
                stream = command.lines(**_lines_kwargs())
                with pytest.raises(_StartHookError) as caught:
                    await asyncio.wait_for(anext(stream), _WATCHDOG_S)
            assert caught.value is sentinel, (
                f"the hook's own error must reach the caller, got {caught.value!r}"
            )
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
        finally:
            if stream is not None:
                await stream.aclose()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())


def test_a_cancellation_at_the_heartbeat_stop_still_releases(
    owned_builder: cabc.Callable[..., SafeCmd],
    spawn_backend: BackendTraversal,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancellation during the heartbeat stop must not skip the reclaim.

    ``_abandon_unstarted_run`` stops the idle heartbeat before it enters either
    reclaim path, and that stop awaits the monitor's own cancellation. A
    cancellation arriving there would otherwise escape with the child still
    running and both inherited descriptors owned by nothing the caller can
    reach, because no later route holds the abandoned run. The abandonment is
    one shielded sequence, so it completes before the cancellation is
    re-raised.

    The gate is the heartbeat stop itself, so the cancellation is provably
    delivered there rather than to whatever the scheduler happened to be
    doing.
    """
    context = f"a cancellation at the heartbeat stop on {spawn_backend.requested}"
    pid_file = tmp_path / "heartbeat-cancel.pid"
    command = _holder_command(owned_builder, pid_file)
    sentinel = _StartHookError("the start hook failed")
    record = record_spawn(monkeypatch, "cuprum._line_stream.spawn")
    gate = Gate()
    real_stop = line_stream_spawn._stop_idle_monitor

    async def gated_stop(idle: _IdleMonitor | None) -> None:
        """Hold the heartbeat stop open so the cancellation lands on it."""
        await gate.hold()
        await real_stop(idle)

    monkeypatch.setattr(line_stream_spawn, "_stop_idle_monitor", gated_stop)

    async def run_case() -> None:
        """Fail the start hook, cancel at the heartbeat stop, read the fds."""
        descendant_pid: int | None = None
        stream: LineStream | None = None
        try:
            with sh.observe(_failing_start_hook(sentinel, pid_file)):
                stream = command.lines(**_lines_kwargs(idle_after=30.0))
                task = asyncio.ensure_future(anext(stream))
                await reach_gate_and_cancel(task, gate, context=context)
            descendant_pid = assert_released_before_the_descendant_ends(
                record, pid_file, context=context
            )
        finally:
            if stream is not None:
                await stream.aclose()
            reap_descendant(pid_file, descendant_pid)

    asyncio.run(run_case())
