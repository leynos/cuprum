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

import pytest

from cuprum import RunOutputOptions, ScopeConfig, scoped
from cuprum.sh import ExecutionContext, LineStream
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.lines import LineEvent
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
    )


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
        assert stream.result is not None
        return events, stream.result

    events, result = asyncio.run(collect())

    stdout = [event.text for event in events if event.stream == "stdout"]
    stderr = [event.text for event in events if event.stream == "stderr"]
    assert stdout == ["o1", "o2"], f"stdout order broken: {stdout!r}"
    assert stderr == ["e1", "e2"], f"stderr order broken: {stderr!r}"
    stamps = [event.at for event in events]
    assert stamps == sorted(stamps), f"at must be non-decreasing: {stamps!r}"
    assert all(event.at >= 0.0 for event in events)
    assert result is not None
    assert result.ok


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
        assert stream.result is not None
        return stream.result

    result = asyncio.run(iterate())

    assert result is not None
    assert result.ok
    assert result.stdout == "captured line\n", (
        f"capture must survive iteration, got {result.stdout!r}"
    )
    assert sink.getvalue() == "captured line\n", (
        f"echo must survive iteration, got {sink.getvalue()!r}"
    )


def test_lines_on_line_callback_receives_events(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The on_line option delivers LineEvents during run()."""
    events: list[LineEvent] = []
    command = python_builder(
        "-c",
        "import sys; print('cb out'); print('cb err', file=sys.stderr)",
    )

    asyncio.run(
        command.run(
            output=RunOutputOptions(
                on_line=events.append,
            ),
            context=ExecutionContext(
                stderr_sink=typ.cast("typ.IO[str]", io.StringIO()),
            ),
        )
    )

    texts = {(event.stream, event.text) for event in events}
    assert ("stdout", "cb out") in texts
    assert ("stderr", "cb err") in texts
    assert all(event.at >= 0.0 for event in events)


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
    assert result is not None
    assert result.stdout is None, "capture=False must leave stdout unset"
    assert result.stderr is None, "capture=False must leave stderr unset"


def test_line_event_queue_is_finite() -> None:
    """The driver's queue is bounded, so retention cannot grow without limit."""
    from cuprum._line_stream import _LINE_QUEUE_CAPACITY, _line_event_queue

    assert _LINE_QUEUE_CAPACITY > 0, "a bounded queue needs a positive capacity"
    assert _line_event_queue().maxsize == _LINE_QUEUE_CAPACITY, (
        "the driver must consume the bounded queue, not an unbounded one"
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
        assert line == "line"
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

    assert ("stdout", "o1") in observed
    assert ("stdout", "o2") in observed
    assert ("stderr", "e1") in observed
    assert ("stderr", "e2") in observed
    assert len(observed) == 4


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
