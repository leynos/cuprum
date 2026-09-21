"""Unit and behaviour tests for ``SafeCmd.lines()`` line-level iteration.

Covers the three acceptance behaviours from #357: per-stream ordering with a
monotonic ``at`` stamp, capture and echo coexisting with iteration, and the
cancellation teardown. The behaviour-level scenario (iterating lines and
checking stream tags and text) lives at the bottom.

Also pins the teardown contract that ``async for`` alone does not provide:
``break`` leaves the child running and only ``aclose()`` — directly or through
``async with`` — ends it; the bounded queue's backpressure seam, which is what
keeps a chatty child from being buffered without limit; and line observation
with both capture and echo off, which keeps the pipes open on its own.
"""

from __future__ import annotations

import asyncio
import io
import sys
import typing as typ
from collections import Counter

import pytest

from cuprum import RunOutputOptions, ScopeConfig, before, observe, scoped
from cuprum.sh import ExecutionContext, LineStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent, LineStreamName
    from cuprum.sh import CommandResult, SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _two_stream_script() -> str:
    """Script emitting interleaved stdout and stderr lines."""
    return (
        "import sys\n"
        "print('o1', flush=True)\n"
        "print('e1', file=sys.stderr, flush=True)\n"
        "print('o2', flush=True)\n"
        "print('e2', file=sys.stderr, flush=True)\n"
        "print('o1', flush=True)\n"
        "print('e1', file=sys.stderr, flush=True)\n"
    )


def _assert_line_event_order(
    events: cabc.Sequence[LineEvent],
    stream: LineStreamName,
    expected: cabc.Sequence[str],
) -> None:
    """Assert the decoded line order for one output stream."""
    assert [event.text for event in events if event.stream == stream] == list(
        expected
    ), f"line events must preserve {stream} order, got {events!r}"


def _assert_exec_event_order(
    events: cabc.Sequence[ExecEvent],
    stream: LineStreamName,
    expected: cabc.Sequence[str],
) -> None:
    """Assert the observed output-line order for one stream."""
    assert [event.line for event in events if event.phase == stream] == list(
        expected
    ), f"execution events must preserve {stream} order, got {events!r}"


# The child records its own pid, announces readiness with a flushed line, then
# blocks far longer than any test timeout. A child that is still running can
# therefore only have been left running, never merely not-yet-exited, which is
# what makes the teardown assertions below meaningful rather than timed.
_BLOCKING_CHILD = "\n".join((
    "import os, pathlib, time",
    "pathlib.Path(os.environ['PID']).write_text(str(os.getpid()))",
    "print('ready', flush=True)",
    "time.sleep(300)",
))


def _blocking_command(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> tuple[SafeCmd, Path]:
    """Build a command that writes its pid, prints a line, then blocks.

    Returns
    -------
    tuple[SafeCmd, Path]
        The command, and the file its child writes its pid to. The pid is
        written before the child's first line, so a caller that has received a
        line can read the file without polling.
    """
    script = tmp_path / "child.py"
    script.write_text(_BLOCKING_CHILD, encoding="utf-8")
    return python_builder(str(script)), tmp_path / "pid"


def test_lines_preserve_per_stream_order_with_monotonic_at(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Each stream's lines arrive in order, and ``at`` never decreases."""
    command = python_builder("-c", _two_stream_script())

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate the stream, then return events and the result."""
        stream = command.lines()
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    stdout = [event.text for event in events if event.stream == "stdout"]
    stderr = [event.text for event in events if event.stream == "stderr"]
    assert stdout == ["o1", "o2", "o1"], f"stdout order broken: {stdout!r}"
    assert stderr == ["e1", "e2", "e1"], f"stderr order broken: {stderr!r}"
    stamps = [event.at for event in events]
    assert stamps == sorted(stamps), f"at must be non-decreasing: {stamps!r}"
    assert all(event.at >= 0.0 for event in events), (
        f"line timestamps must be non-negative, got {stamps!r}"
    )
    assert result is not None, "line iteration must return a command result"
    assert result.ok, f"line iteration command must succeed, got {result!r}"


def test_lines_publish_nonzero_result_with_captured_output(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A completed non-zero command exposes its lines and captured result."""
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)",
    )

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate to completion and retain the non-zero command result."""
        stream = command.lines()
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed non-zero line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    assert Counter((event.stream, event.text) for event in events) == {
        ("stdout", "out"): 1,
        ("stderr", "err"): 1,
    }, f"line iteration must retain both output streams, got {events!r}"
    assert result.exit_code == 7, f"non-zero exit code must be retained, got {result!r}"
    assert not result.ok, f"non-zero command result must not be ok, got {result!r}"
    assert result.stdout == "out\n", (
        f"stdout capture must survive non-zero completion, got {result.stdout!r}"
    )
    assert result.stderr == "err\n", (
        f"stderr capture must survive non-zero completion, got {result.stderr!r}"
    )


def test_lines_keep_capture_and_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Iterating lines does not disable capture or echo."""
    sink = io.StringIO()
    command = python_builder("-c", "print('captured line')")

    async def iterate() -> CommandResult:
        """Iterate with capture and echo on, then return the result."""
        stream = command.lines(
            output=RunOutputOptions(capture=True, echo=True),
            context=ExecutionContext(stdout_sink=typ.cast("typ.IO[str]", sink)),
        )
        async for _event in stream:
            pass
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return stream.result

    result = asyncio.run(iterate())

    assert result is not None, "line iteration must return a command result"
    assert result.ok, f"line iteration command must succeed, got {result!r}"
    assert result.stdout == "captured line\n", (
        f"capture must survive iteration, got {result.stdout!r}"
    )
    assert sink.getvalue() == "captured line\n", (
        f"echo must survive iteration, got {sink.getvalue()!r}"
    )


def test_lines_observe_without_capture_or_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Line iteration keeps both pipes open when it is their only consumer."""
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Collect line events and the completed stream result."""
        stream = command.lines(output=RunOutputOptions(capture=False, echo=False))
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a completed line stream must expose its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    observed = {(event.stream, event.text) for event in events}
    assert observed == {("stdout", "out"), ("stderr", "err")}, (
        f"lines() must observe both streams with capture and echo off, got {observed!r}"
    )
    assert result.stdout is None, "capture=False must leave stdout unset"
    assert result.stderr is None, "capture=False must leave stderr unset"


def test_lines_defer_hooks_until_iteration(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Constructing or closing an unstarted stream runs no execution hooks."""
    command = python_builder("-c", "print('unused')")
    before_calls: list[SafeCmd] = []
    phases: list[str] = []

    def before_hook(cmd: SafeCmd) -> None:
        """Record a deferred before-hook invocation."""
        before_calls.append(cmd)

    def observe_hook(event: ExecEvent) -> None:
        """Record an observe-hook event phase."""
        phases.append(event.phase)

    with (
        scoped(ScopeConfig(allowlist=frozenset([command.program]))),
        before(before_hook),
        observe(observe_hook),
    ):
        stream = command.lines()
        assert not before_calls, "constructing lines() must not run before hooks"
        assert not phases, "constructing lines() must not emit a plan event"
        asyncio.run(stream.aclose())

    assert not before_calls, "closing an unstarted stream must not run before hooks"
    assert not phases, "closing an unstarted stream must not emit observe events"


def test_lines_started_stream_emits_lifecycle_and_output_events(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Starting iteration emits one correlated lifecycle with both outputs."""
    command = python_builder("-c", _two_stream_script())
    observed: list[ExecEvent] = []

    async def collect() -> list[LineEvent]:
        """Iterate the started stream while observation is active."""
        stream = command.lines()
        return [event async for event in stream]

    with observe(observed.append):
        lines = asyncio.run(collect())

    output_events = [event for event in observed if event.phase in {"stdout", "stderr"}]
    assert [event.phase for event in observed[:2]] == ["plan", "start"], (
        f"iteration must plan and start before output, got {observed!r}"
    )
    assert observed[-1].phase == "exit", (
        f"completion must follow all output, got {observed!r}"
    )
    assert observed.index(observed[-1]) > max(
        index
        for index, event in enumerate(observed)
        if event.phase in {"stdout", "stderr"}
    ), "exit must follow every output event"
    assert len({event.exec_id for event in observed}) == 1, (
        f"one line-stream run must share one exec_id, got {observed!r}"
    )
    _assert_exec_event_order(output_events, "stdout", ["o1", "o2", "o1"])
    _assert_exec_event_order(output_events, "stderr", ["e1", "e2", "e1"])
    _assert_line_event_order(lines, "stdout", ["o1", "o2", "o1"])
    _assert_line_event_order(lines, "stderr", ["e1", "e2", "e1"])


def test_lines_on_line_callback_receives_events(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Both output channels see ordered lines from one real subprocess."""
    line_events: list[LineEvent] = []
    observe_events: list[ExecEvent] = []
    command = python_builder("-c", _two_stream_script())

    with observe(observe_events.append):
        asyncio.run(
            command.run(
                output=RunOutputOptions(on_line=line_events.append),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", io.StringIO()),
                ),
            )
        )

    expected = Counter({
        ("stdout", "o1"): 2,
        ("stdout", "o2"): 1,
        ("stderr", "e1"): 2,
        ("stderr", "e2"): 1,
    })
    output_events = [
        event for event in observe_events if event.phase in {"stdout", "stderr"}
    ]
    assert Counter((event.stream, event.text) for event in line_events) == expected, (
        f"on_line must receive every decoded line, got {line_events!r}"
    )
    assert Counter((event.phase, event.line) for event in output_events) == expected, (
        f"observe must receive every output event, got {output_events!r}"
    )
    _assert_line_event_order(line_events, "stdout", ["o1", "o2", "o1"])
    _assert_line_event_order(line_events, "stderr", ["e1", "e2", "e1"])
    _assert_exec_event_order(output_events, "stdout", ["o1", "o2", "o1"])
    _assert_exec_event_order(output_events, "stderr", ["e1", "e2", "e1"])
    assert all(event.at >= 0.0 for event in line_events), (
        f"line callback timestamps must be non-negative, got {line_events!r}"
    )
    assert [event.at for event in line_events] == sorted(
        event.at for event in line_events
    ), f"line callback timestamps must be non-decreasing, got {line_events!r}"


def test_lines_cancellation_kills_child(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """Cancelling mid-iteration tears the child down; iteration must not hang."""
    if sys.platform == "win32":  # pragma: no cover - POSIX signals only
        pytest.skip("Cancellation escalation semantics rely on POSIX signals")
    from tests.helpers.timeouts import wait_for_process_death

    command, pid_file = _blocking_command(python_builder, tmp_path)

    async def orchestrate() -> int:
        """Iterate one line, then cancel the consuming task."""
        stream = command.lines(
            context=ExecutionContext(env={"PID": str(pid_file)}, cancel_grace=0.1),
        )
        consumer = asyncio.create_task(_consume_to_exhaustion(stream))
        deadline = asyncio.get_running_loop().time() + 5.0
        while not pid_file.exists():
            if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
                pytest.fail("child never wrote its pid")
            await asyncio.sleep(0.05)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
        return int(pid_file.read_text())

    pid = asyncio.run(orchestrate())
    wait_for_process_death(pid, seconds=2.0, context="lines() cancellation")


async def _consume_to_exhaustion(stream: LineStream) -> None:
    """Iterate *stream* to exhaustion, which a blocking child never reaches.

    Cancellation can only land on a consumer that is still iterating, so this
    deliberately outlives the child instead of breaking after one line: a
    consumer that has already closed the stream finishes first and the
    cancellation becomes unobservable.
    """
    async for _event in stream:
        pass


def test_lines_break_alone_leaves_the_child_running(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """``break`` does not close the stream, so the child keeps running.

    ``async for`` never closes a custom async iterator on ``break``, so leaving
    the loop is not a teardown. The child survives until the stream is closed.
    """
    if sys.platform == "win32":  # pragma: no cover - POSIX signals only
        pytest.skip("Cancellation escalation semantics rely on POSIX signals")
    from tests.helpers.timeouts import (
        process_is_running,
        wait_for_process_death,
    )

    command, pid_file = _blocking_command(python_builder, tmp_path)

    async def orchestrate() -> int:
        """Iterate one line, break, and check the child is untouched."""
        stream = command.lines(
            context=ExecutionContext(env={"PID": str(pid_file)}, cancel_grace=0.1),
        )
        try:
            async for _event in stream:
                break
            pid = int(pid_file.read_text())
            assert process_is_running(pid), (
                "breaking out of iteration must not, on its own, stop the child"
            )
            return pid
        finally:
            await stream.aclose()

    pid = asyncio.run(orchestrate())
    wait_for_process_death(pid, seconds=5.0, context="lines() aclose()")


def test_lines_async_with_closes_and_tears_down(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """Leaving an ``async with`` block closes the stream and reaps the child."""
    if sys.platform == "win32":  # pragma: no cover - POSIX signals only
        pytest.skip("Cancellation escalation semantics rely on POSIX signals")
    from tests.helpers.timeouts import (
        process_is_running,
        wait_for_process_death,
    )

    command, pid_file = _blocking_command(python_builder, tmp_path)

    async def orchestrate() -> int:
        """Iterate one line inside a context manager, then leave the block."""
        async with command.lines(
            context=ExecutionContext(env={"PID": str(pid_file)}, cancel_grace=0.1),
        ) as stream:
            async for _event in stream:
                break
            pid = int(pid_file.read_text())
            assert process_is_running(pid), (
                "the child must outlive the loop body; the block exit ends it"
            )
        return pid

    # A bare ``aclose()`` from ``__aexit__`` must not surface a CancelledError
    # the caller never issued, so reaching here at all is part of the contract.
    pid = asyncio.run(orchestrate())
    wait_for_process_death(pid, seconds=5.0, context="lines() async with exit")


def test_run_on_line_observes_without_capture_or_echo(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """``on_line`` alone keeps both pipes open when capture and echo are off.

    Without it, stdout and stderr are attached to ``DEVNULL`` and the callback
    would silently never fire.
    """
    events: list[LineEvent] = []
    command = python_builder(
        "-c",
        "import sys; print('out'); print('err', file=sys.stderr)",
    )

    result = asyncio.run(
        command.run(
            output=RunOutputOptions(capture=False, echo=False, on_line=events.append),
        )
    )

    observed = {(event.stream, event.text) for event in events}
    assert observed == {("stdout", "out"), ("stderr", "err")}, (
        f"on_line must observe both streams with capture and echo off, got {observed!r}"
    )
    assert result is not None, "run() must return a completed command result"
    assert result.stdout is None, "capture=False must leave stdout unset"
    assert result.stderr is None, "capture=False must leave stderr unset"


def test_line_event_queue_is_finite() -> None:
    """The driver's queue is bounded, so retention cannot grow without limit."""
    from cuprum._line_stream import _LINE_QUEUE_CAPACITY, _line_event_queue

    assert _LINE_QUEUE_CAPACITY > 0, "a bounded queue needs a positive capacity"
    assert _line_event_queue().maxsize == _LINE_QUEUE_CAPACITY, (
        "the driver must consume the bounded queue, not an unbounded one"
    )


def test_lines_stamp_events_from_the_spawn_clock(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The line event clock subtracts the reference stamped after spawning."""
    from cuprum import _line_callbacks
    from cuprum._line_stream import coordinator

    monkeypatch.setattr(coordinator, "perf_counter", lambda: 40.0)
    monkeypatch.setattr(_line_callbacks, "perf_counter", lambda: 43.25)
    command = python_builder("-c", "print('clock')")

    async def collect() -> list[LineEvent]:
        """Iterate the command and retain its one clock-stamped event."""
        return [event async for event in command.lines()]

    events = asyncio.run(collect())

    assert [event.at for event in events] == [3.25], (
        f"line timestamps must use the injected spawn reference, got {events!r}"
    )


def test_queue_line_sink_applies_backpressure() -> None:
    """A full queue parks the sink rather than raising or dropping the event."""
    from cuprum._line_stream import _line_event_queue, _queue_line_sink
    from cuprum.lines import LineEvent

    async def exercise() -> None:
        """Fill the queue, then confirm the sink waits for a free slot."""
        queue = _line_event_queue()
        sink = _queue_line_sink(queue)
        for index in range(queue.maxsize):
            queue.put_nowait(
                LineEvent(stream="stdout", at=0.0, text=str(index)),
            )

        parked = sink(LineEvent(stream="stdout", at=0.0, text="overflow"))
        assert parked is not None, (
            "the queue sink applies backpressure, so it answers with an awaitable"
        )
        blocked = asyncio.ensure_future(parked)
        await asyncio.sleep(0)
        assert not blocked.done(), (
            "a full queue must hold the sink instead of raising QueueFull"
        )

        drained = queue.get_nowait()
        assert isinstance(drained, LineEvent), "only line events are queued"
        assert drained.text == "0", "the queue drains in arrival order"
        await blocked
        assert queue.qsize() == queue.maxsize, (
            "the parked event must be delivered once a slot frees up"
        )

    asyncio.run(exercise())


def test_emit_line_awaits_an_asynchronous_sink() -> None:
    """A sink returning an awaitable holds the drain loop until it settles.

    That is the seam ``lines()`` relies on: the bounded queue can only push
    back on the child if the reader waits for the sink, because one read can
    carry thousands of lines.
    """
    from cuprum._streams import _emit_line

    started = asyncio.Event()
    released = asyncio.Event()

    async def sink(line: str) -> None:
        """Signal that the sink was entered, then wait to be released."""
        assert line == "line", f"the sink must receive the decoded line, got {line!r}"
        started.set()
        await released.wait()

    async def exercise() -> None:
        """Confirm the drain loop is held while the sink is unsettled."""
        pending = asyncio.ensure_future(_emit_line(sink, "line"))
        await started.wait()
        assert not pending.done(), "the drain loop must wait for the sink"
        released.set()
        await pending

    asyncio.run(exercise())


def test_emit_line_invokes_a_synchronous_sink() -> None:
    """A sink returning ``None`` is called without being awaited."""
    from cuprum._streams import _emit_line

    seen: list[str] = []

    async def exercise() -> None:
        """Emit one line through a plain callback."""
        await _emit_line(seen.append, "line")

    asyncio.run(exercise())

    assert seen == ["line"], "a synchronous sink must still receive every line"


def test_lines_timeout_raises_timeout_expired(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A lines() run translates deadline expiry exactly as run() does."""
    from cuprum.sh import TimeoutExpired

    command = python_builder("-c", "import time; time.sleep(5)")

    async def iterate() -> None:
        """Iterate a command that outlives its deadline."""
        stream = command.lines(timeout=0.1)
        with pytest.raises(TimeoutExpired):
            async for _event in stream:
                pass

    asyncio.run(iterate())


def test_lines_empty_output_completes_and_publishes_result(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A child that writes nothing still completes and publishes its result."""
    command = python_builder("-c", "pass")

    async def collect() -> tuple[list[LineEvent], CommandResult]:
        """Iterate a silent command and retain its published result."""
        stream = command.lines(timeout=2.0)
        events = [event async for event in stream]
        assert stream.result is not None, (
            "a silent line stream must still publish its CommandResult"
        )
        return events, stream.result

    events, result = asyncio.run(collect())

    assert events == [], f"a silent child must yield no line events, got {events!r}"
    assert result.exit_code == 0, (
        f"a silent child must exit cleanly, got exit_code={result.exit_code!r}"
    )
    assert result.ok is True, f"a silent child must succeed, got {result!r}"
    assert not result.stdout, (
        f"a silent child must capture no stdout, got {result.stdout!r}"
    )
    assert not result.stderr, (
        f"a silent child must capture no stderr, got {result.stderr!r}"
    )


def test_lines_behaviour_streams_tags_and_text(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Behaviour: the observed stream tags and text match the child's output."""
    command = python_builder("-c", _two_stream_script())

    async def follow() -> list[tuple[str, str]]:
        """Collect (stream, text) pairs from a full iteration."""
        stream = command.lines()
        return [(event.stream, event.text) async for event in stream]

    observed = asyncio.run(follow())

    assert ("stdout", "o1") in observed, (
        f"stdout line o1 must be observed, got {observed!r}"
    )
    assert ("stdout", "o2") in observed, (
        f"stdout line o2 must be observed, got {observed!r}"
    )
    assert ("stderr", "e1") in observed, (
        f"stderr line e1 must be observed, got {observed!r}"
    )
    assert ("stderr", "e2") in observed, (
        f"stderr line e2 must be observed, got {observed!r}"
    )
    assert len(observed) == 6, f"expected 6 line events, got {observed!r}"


def test_lines_allowlist_is_enforced(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """lines() refuses a program the active context forbids."""
    from cuprum.context import ForbiddenProgramError

    command = python_builder("-c", "print('nope')")

    async def iterate() -> None:
        """Attempt iteration that must raise."""
        stream = command.lines()
        async for _event in stream:
            pass

    with (
        scoped(ScopeConfig(allowlist=frozenset())),  # nothing allowed
        pytest.raises(ForbiddenProgramError),
    ):
        asyncio.run(iterate())
