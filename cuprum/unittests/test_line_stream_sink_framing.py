"""Presentation-sink framing on the ``SafeCmd.lines()`` path.

``lines()`` is lazy, so its sink session cannot be opened when the stream is
built: a stream that is never pulled would leave a GitHub Actions group and its
stop-commands lease open, silencing every workflow command the runner processes
afterwards. It must instead open on the first pull and close on every terminal
path, including an early close and a cancellation.

These are real-child tests because the property under test is ordering between
bytes the child wrote and control lines the adapter wrote around them.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum._sink_lifecycle import _run_label
from cuprum.context import ForbiddenProgramError
from cuprum.sh import BoundedDelivery, CommandResult, RunOutputOptions
from cuprum.sinks.github_actions import _escape_property
from cuprum.unittests._sink_test_support import RecordingSession, RecordingSink
from cuprum.unittests.test_sinks_end_to_end import (
    _ON_CI,
    _annotations,
    _flags_output,
    _forced_sink,
    _python_command,
    _stop_token,
)
from tests.helpers.timeouts import started_pids, wait_for_process_death

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent
    from cuprum.sinks import SessionOutcome

_LEASE_PREFIX = "::stop-commands::"

# The child announces itself and then blocks, so an early close or a
# cancellation is the only way the run can end.
_READY_THEN_BLOCK = "import time; print('ready', flush=True); time.sleep(300)"

# Every workflow command a child could use to escape its own group or forge an
# annotation. All three must reach the log before the lease is released.
_SPOOFING_CHILD = (
    "print('inside the group'); "
    "print('::endgroup::'); "
    "print('::error title=child::spoofed')"
)


async def _drain(
    command: sh.SafeCmd,
    output: RunOutputOptions,
) -> tuple[list[str], CommandResult]:
    """Consume one streamed run to completion and return its lines and result."""
    async with command.lines(output=output) as stream:
        texts = [event.text async for event in stream]
    result = stream.result
    assert isinstance(result, CommandResult), (
        f"a completed line stream must report its result; got {result!r}"
    )
    return texts, result


def _assert_lease_frames(
    value: str,
    command: sh.SafeCmd,
    *child_outputs: str,
) -> None:
    """Assert the group opens first, the lease wraps the child, and both close.

    Parameters
    ----------
    value : str
        The sink buffer or captured stderr after the stream finished.
    command : sh.SafeCmd
        The streamed command; its bounded run label titles the group.
    *child_outputs : str
        Child lines that must fall inside the session's framing.
    """
    assert value.count("::group::") == 1, (
        f"one line stream must open one group; got {value!r}"
    )
    assert value.count(_LEASE_PREFIX) == 1, (
        f"one line stream must open one stop-commands lease; got {value!r}"
    )
    assert value.startswith(f"::group::{_run_label(command)}\n"), (
        f"the group must open before any echoed output; got {value!r}"
    )
    release = f"::{_stop_token(value)}::\n"
    assert value.index(_LEASE_PREFIX) < value.index(release), (
        f"the lease must be released after it was opened; got {value!r}"
    )
    for line in child_outputs:
        assert line in value, f"the child's {line!r} must be echoed; got {value!r}"
        assert value.index(line) < value.index(release), (
            f"{line!r} must precede the lease release so the runner ignores it; "
            f"got {value!r}"
        )
    assert value.rindex("::endgroup::\n") > value.index(release), (
        f"the session's own endgroup must follow the lease release; got {value!r}"
    )


class _BlockingCloseSession(RecordingSession):
    """A recording session whose close waits until the test releases it."""

    def __init__(self, release: threading.Event) -> None:
        """Record where the close runs, and hold it open until released."""
        super().__init__()
        self.release = release
        self.close_started = threading.Event()
        self.close_finished = threading.Event()
        self.close_thread: str | None = None

    def close(self, outcome: SessionOutcome) -> None:
        """Record the closing thread, then wait for the release."""
        self.close_thread = threading.current_thread().name
        self.close_started.set()
        try:
            self.release.wait(timeout=5.0)
        finally:
            self.close_finished.set()
        super().close(outcome)


def test_grouped_lines_lease_wraps_live_child_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``group=True`` shields child output on the line-iteration path too.

    The runner only ignores workflow commands between the lease and its
    release, so every line the child writes must land inside that window. This
    is the regression the issue reports: the line path used to leave the sink
    unopened, letting the child's ``::endgroup::`` and forged annotation reach
    the job log as commands.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command(_SPOOFING_CHILD)

    with scoped(ScopeConfig(allowlist=allowlist)):
        texts, _ = asyncio.run(_drain(command, _flags_output(group=True)))

    assert texts == [
        "inside the group",
        "::endgroup::",
        "::error title=child::spoofed",
    ], f"line events must keep their content and order under framing; got {texts!r}"
    _assert_lease_frames(
        capsys.readouterr().err,
        command,
        "inside the group",
        "::endgroup::",
        "::error title=child::spoofed",
    )


def test_stream_built_but_not_pulled_frames_nothing() -> None:
    """Closing an unpulled stream must not emit a group or a lease."""
    command, allowlist = _python_command("print('never started')")
    sink, buffer = _forced_sink()

    async def abandon() -> None:
        """Build a stream and close it without requesting an event."""
        stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
        await stream.aclose()

    with scoped(ScopeConfig(allowlist=allowlist)):
        asyncio.run(abandon())

    framed = buffer.getvalue()
    assert not framed, f"a stream that never ran must frame nothing; got {framed!r}"


def test_early_close_releases_lease_and_terminates_child() -> None:
    """An explicit close releases the lease and reaps the still-running child."""
    command, allowlist = _python_command(_READY_THEN_BLOCK)
    sink, buffer = _forced_sink()
    events: list[ExecEvent] = []

    async def close_after_first_line() -> None:
        """Stop consuming after one line, then close the stream."""
        with (
            scoped(ScopeConfig(allowlist=allowlist)),
            sh.observe(events.append),
        ):
            stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
            try:
                first = await anext(stream)
                assert first.text == "ready", (
                    f"the child must start before the close; got {first!r}"
                )
            finally:
                await stream.aclose()

    asyncio.run(close_after_first_line())

    (pid,) = started_pids(events)
    wait_for_process_death(pid, seconds=5.0, context="an early-closed line stream")
    _assert_lease_frames(buffer.getvalue(), command, "ready")


def test_cancelled_consumer_releases_lease_and_terminates_child() -> None:
    """Cancelling an awaiting consumer tears down child and framing alike."""
    command, allowlist = _python_command(_READY_THEN_BLOCK)
    sink, buffer = _forced_sink()
    events: list[ExecEvent] = []

    async def cancel_while_waiting() -> None:
        """Park a second read on the live child, then cancel it."""
        with (
            scoped(ScopeConfig(allowlist=allowlist)),
            sh.observe(events.append),
        ):
            stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
            first = await anext(stream)
            assert first.text == "ready", (
                f"the child must start before the cancellation; got {first!r}"
            )
            waiter = asyncio.ensure_future(anext(stream))
            # One turn lets the waiter reach the queue read; the child emits
            # nothing further, so it stays parked there.
            await asyncio.sleep(0)
            assert not waiter.done(), (
                "the waiter must be parked on the live child before it is cancelled"
            )
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            await stream.aclose()

    asyncio.run(cancel_while_waiting())

    (pid,) = started_pids(events)
    wait_for_process_death(pid, seconds=5.0, context="a cancelled line stream")
    _assert_lease_frames(buffer.getvalue(), command, "ready")


def test_completed_stream_closes_as_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A completed line stream must not annotate itself as cancelled.

    ``LineStream`` closes its generator after the terminal result, which throws
    ``GeneratorExit`` into it. That ordinary close says nothing about how the
    child ended: the session must close with the exit outcome, or an exit-zero
    run annotates a failure the child never reported.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("print('done')")

    with scoped(ScopeConfig(allowlist=allowlist)):
        _, result = asyncio.run(_drain(command, _flags_output(annotate_failure=True)))

    assert result.exit_code == 0, f"the child must succeed; got {result!r}"
    assert _annotations(capsys.readouterr().err) == [], (
        "an exit-zero line stream must not annotate a failure"
    )


def test_failed_stream_annotates_exit_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed line stream annotates its exit, not a cancellation.

    The annotation detail is the only place the outcome reaches a user, so a
    completed run whose child failed has to report ``exit_nonzero``; reporting
    ``cancelled`` would blame the caller for a failure the child chose.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("raise SystemExit(7)")

    with scoped(ScopeConfig(allowlist=allowlist)):
        _, result = asyncio.run(_drain(command, _flags_output(annotate_failure=True)))

    assert result.exit_code == 7, f"the child must exit 7; got {result!r}"
    assert _annotations(capsys.readouterr().err) == [
        f"title={_escape_property(_run_label(command))}::exit_nonzero",
    ], "a failed line stream must annotate its categorical exit outcome"


def test_denied_first_pull_opens_no_sink_session() -> None:
    """The opener enforces the allowlist before it frames anything.

    ``run()`` refuses a forbidden program before writing its group line, and a
    pull does the same: opening first would let a denied run publish framing
    the caller never authorized, so a regression that reordered the check would
    otherwise pass every framing test.
    """
    command, allowlist = _python_command("print('never runs')")
    sink = RecordingSink()

    async def pull(stream: sh.LineStream) -> None:
        """Request the first event under the restricted scope."""
        with pytest.raises(ForbiddenProgramError):
            await anext(stream)
        await stream.aclose()

    # Build permissively, then deny: authorization is deferred to the pull,
    # and the opener must refuse before it frames anything.
    with scoped(ScopeConfig(allowlist=allowlist)):
        stream = command.lines(output=RunOutputOptions(echo=True, sink=sink))
    with scoped(ScopeConfig(allowlist=frozenset())):
        asyncio.run(pull(stream))

    assert sink.opened == 0, (
        "a denied first pull must not open the presentation sink, so no group "
        f"or lease is written; the adapter was consulted {sink.opened} time(s)"
    )


def test_timed_out_stream_annotates_timeout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A line-stream timeout annotates its categorical detail.

    The ``run()`` timeout annotation is covered elsewhere; the line path maps
    the failure through the same session close, so it must report ``timeout``
    rather than the run's error category.
    """
    monkeypatch.setenv("GITHUB_ACTIONS", _ON_CI)
    command, allowlist = _python_command("import time; time.sleep(5)")

    async def consume() -> None:
        """Consume a child that outlives its deadline."""
        async with command.lines(
            output=_flags_output(annotate_failure=True),
            timeout=0.2,
        ) as stream:
            with pytest.raises(sh.TimeoutExpired):
                async for _event in stream:
                    pass

    with scoped(ScopeConfig(allowlist=allowlist)):
        asyncio.run(consume())

    assert _annotations(capsys.readouterr().err) == [
        f"title={_escape_property(_run_label(command))}::timeout",
    ], "a timed-out line stream must annotate its categorical timeout"


def test_bounded_delivery_closes_the_session_off_loop() -> None:
    """A bounded line stream finalizes its session on the delivery closer.

    The unbounded close runs inline on the event loop. Bounded delivery
    instead starts a daemon closer and waits only for the drain grace, so a
    held close proves both branches: the stream finishes while the close is
    still blocked, and the close runs on the closer thread rather than the
    loop's.
    """
    command, allowlist = _python_command("print('done')")
    release = threading.Event()
    session = _BlockingCloseSession(release)
    sink = RecordingSink(session_factory=lambda: session)
    output = RunOutputOptions(
        echo=True,
        sink=sink,
        delivery=BoundedDelivery(max_queued_bytes=128, drain_grace_s=0.05),
    )

    async def consume() -> CommandResult:
        """Complete the stream, then inspect the still-held close."""
        result: CommandResult | None = None
        try:
            _, result = await asyncio.wait_for(_drain(command, output), timeout=2.0)
            started = await asyncio.to_thread(session.close_started.wait, 2.0)
            assert started, "the bounded closer must enter the session close"
            assert session.close_thread == "cuprum-output-session-close", (
                "a bounded run must close its session on the closer thread, not "
                f"the event loop; got {session.close_thread!r}"
            )
            assert session.closed == 0, (
                "the stream must return before the held close finishes"
            )
        finally:
            release.set()
        finished = await asyncio.to_thread(session.close_finished.wait, 2.0)
        assert finished, "the released close must finish"
        assert result is not None, "the completed stream must report its result"
        return result

    with scoped(ScopeConfig(allowlist=allowlist)):
        result = asyncio.run(consume())

    assert result.exit_code == 0, f"the child must succeed; got {result!r}"
    assert session.closed == 1, "the session must close exactly once"
