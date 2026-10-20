"""Process teardown and completion contracts for ``SafeCmd.lines()``."""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped
from cuprum.sh import ExecutionContext, LineStream
from cuprum.unittests._safe_cmd_lines_support import _blocking_command

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.lines import LineEvent
    from cuprum.sh import CommandResult, SafeCmd


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
