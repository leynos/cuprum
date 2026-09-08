"""Unit and behaviour tests for ``SafeCmd.lines()`` line-level iteration.

Covers the three acceptance behaviours from #357: per-stream ordering with a
monotonic ``at`` stamp, capture and echo coexisting with iteration, and the
cancellation teardown. The behaviour-level scenario (iterating lines and
checking stream tags and text) lives at the bottom.
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

    pid_file = tmp_path / "pid"
    script = tmp_path / "child.py"
    script.write_text(
        "\n".join(
            (
                "import os, pathlib, time",
                "pathlib.Path(os.environ['PID']).write_text(str(os.getpid()))",
                "print('ready', flush=True)",
                "time.sleep(30)",
            ),
        ),
        encoding="utf-8",
    )
    command = python_builder(str(script))

    async def orchestrate() -> int:
        """Iterate one line, then cancel the consuming task."""
        stream = command.lines(
            context=ExecutionContext(env={"PID": str(pid_file)}, cancel_grace=0.1),
        )
        consumer = asyncio.create_task(_consume_one(stream))
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


async def _consume_one(stream: LineStream) -> int:
    """Consume exactly one line from *stream*, then close it."""
    count = 0
    try:
        async for _event in stream:
            count += 1
            break
    finally:
        await stream.aclose()
    return count


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
