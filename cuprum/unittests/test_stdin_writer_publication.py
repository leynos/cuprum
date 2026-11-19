"""Pin when the payload stdin writer publishes its failure.

The exit race asks the run's ``_StdinWriterLog`` because a failed writer is
still *running* while it unwinds, and the payload writer's unwinding can
suspend: its pipe close waits, within a bound, for the child-side reader to
reach EOF. A failure published after that close would therefore be invisible
to a race that asks during it, so the writer hands the failure over first.

This module drives that ordering without a child. The double's ``wait_closed``
parks until the case releases it, so the assertion is made at a point where
the close has demonstrably begun and cannot yet have finished.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._subprocess_rendezvous import _StdinWriterLog
from cuprum._subprocess_stdin import _write_stdin

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation

_WATCHDOG_S = 10.0


class _SentinelError(Exception):
    """The failure a double raises, identified by class rather than message.

    Deliberately neither an ``OSError`` nor a ``RuntimeError``: those are the
    pipe failures the writer classifies and swallows, and this case is the
    other kind — a failure that escapes the write and must reach the caller.
    """


class _ParkingStdin:
    """Stdin transport whose close blocks until the case permits it."""

    def __init__(self, close_started: asyncio.Event, release: asyncio.Event) -> None:
        """Record the events that coordinate the parked close."""
        self._close_started = close_started
        self._release = release
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        """Accept the payload without buffering semantics."""
        self.written += data

    async def drain(self) -> None:
        """Complete immediately; the payload always fits."""
        await asyncio.sleep(0)

    def close(self) -> None:
        """Record the graceful close."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Park until the case releases the close, signalling its entry first."""
        self._close_started.set()
        await self._release.wait()


class _RaisingObservation:
    """Observation whose emission is the writer's injected failure."""

    def __init__(self, failure: BaseException) -> None:
        """Hold the failure every emission raises."""
        self._failure = failure
        self.events: list[str] = []

    def emit(self, phase: str, details: object) -> None:
        """Record the phase, then fail as caller code would."""
        _ = details
        self.events.append(phase)
        raise self._failure


class _FakeProcess:
    """Process double exposing the attributes ``_write_stdin`` reads."""

    def __init__(self, stdin: _ParkingStdin) -> None:
        """Attach the stdin transport and a fixed pid."""
        self.stdin = stdin
        self.pid = 123


def test_payload_writer_publishes_its_failure_before_the_close_suspends() -> None:
    """The failure is in the log while the close is parked, not after it.

    The observation raises after the write and drain succeed, which is the
    shape of a failure escaping cuprum's own diagnostic rather than the pipe.
    Once the close has begun and is suspended, the log must already hold that
    failure: reading it later would let a race select the consumer failure this
    writer's own unwinding causes instead of the writer's own.
    """

    async def _drive() -> None:
        """Start the writer, inspect the log while its close is parked."""
        close_started = asyncio.Event()
        release = asyncio.Event()
        stdin = _ParkingStdin(close_started, release)
        failure = _SentinelError("payload writer failure")
        observation = _RaisingObservation(failure)
        log = _StdinWriterLog()
        process = typ.cast("asyncio.subprocess.Process", _FakeProcess(stdin))

        task = asyncio.create_task(
            _write_stdin(
                process,
                b"payload",
                typ.cast("_StageObservation", observation),
                log,
            )
        )
        await asyncio.wait_for(close_started.wait(), _WATCHDOG_S)

        assert log.error is failure, (
            "the writer must publish its failure before the close suspends, "
            f"found {type(log.error).__name__ if log.error else None}"
        )
        assert not task.done(), (
            "the close must still be parked when the log is inspected, or the "
            "case cannot witness that publication preceded it"
        )

        release.set()
        with pytest.raises(_SentinelError):
            await asyncio.wait_for(task, _WATCHDOG_S)

    asyncio.run(_drive())
