"""Bounded session finalization stays ordered behind blocked output writes."""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum import ScopeConfig, TimeoutExpired, scoped, sh
from cuprum.sh import BoundedDelivery, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.sh import Pipeline, SafeCmd
    from cuprum.sinks import OutputSession, SessionOutcome, SessionStart


class _FramedDestination:
    """A frame sink that blocks only on child output until released."""

    def __init__(self) -> None:
        """Create the framed log and controlled child-write gates."""
        self.child_started = threading.Event()
        self.release_child = threading.Event()
        self.close_started = threading.Event()
        self.close_finished = threading.Event()
        self._messages: list[str] = []
        self._message_lock = threading.Lock()

    @property
    def messages(self) -> tuple[str, ...]:
        """Framed writes in the order the destination accepted."""
        with self._message_lock:
            return tuple(self._messages)

    def write(self, text: str, /) -> int:
        """Block the child record but allow opening and closing frames."""
        if text == "payload\n":
            self.child_started.set()
            if not self.release_child.wait(timeout=10.0):
                raise TimeoutError
        elif text == "CLOSE\n":
            self.close_started.set()
        with self._message_lock:
            self._messages.append(text)
        if text == "CLOSE\n":
            self.close_finished.set()
        return len(text)

    def flush(self) -> None:
        """Match the small writable text-sink contract."""


class _FramedSession:
    """A minimal frame owner that emits explicit opening and closing records."""

    def __init__(self, destination: _FramedDestination) -> None:
        """Write the opening frame before the run begins."""
        self._destination = destination
        self._destination.write("OPEN\n")

    @property
    def log(self) -> typ.IO[str]:
        """Ordered destination for child output."""
        return typ.cast("typ.IO[str]", self._destination)

    def close(self, outcome: SessionOutcome) -> None:
        """Append the terminal frame after any in-flight child record."""
        del outcome
        self._destination.write("CLOSE\n")


class _FramedSink:
    """Open one framed session over the controllable destination."""

    def __init__(self, destination: _FramedDestination) -> None:
        """Retain the destination used by every session from this adapter."""
        self.destination = destination

    def open_session(self, start: SessionStart) -> OutputSession:
        """Open a session after receiving bounded run metadata."""
        del start
        return _FramedSession(self.destination)


@pytest.mark.parametrize("subject_kind", ["command", "pipeline"])
def test_blocked_framed_sink_does_not_hold_run_or_race_closing_frame(
    subject_kind: str,
) -> None:
    """Timeout returns before release and terminal frame follows the write."""
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)
    producer = python(
        "-c",
        "import time; print('payload', flush=True); time.sleep(30)",
    )
    if subject_kind == "command":
        subject: SafeCmd | Pipeline = producer
    else:
        consumer = python(
            "-c",
            "import sys, time; line = sys.stdin.readline(); "
            "sys.stdout.write(line); "
            "sys.stdout.flush(); time.sleep(30)",
        )
        subject = producer | consumer
    destination = _FramedDestination()
    sink = _FramedSink(destination)

    async def run_subject() -> object:
        """Run with a short timeout while the framed output write blocks."""
        with scoped(ScopeConfig(allowlist=frozenset([program]))):
            return await subject.run(
                timeout=2.0,
                output=RunOutputOptions(
                    capture=True,
                    echo_stdout=True,
                    echo_stderr=False,
                    sink=sink,
                    delivery=BoundedDelivery(
                        max_queued_bytes=128,
                        drain_grace_s=0.02,
                    ),
                ),
                context=ExecutionContext(cancel_grace=0.1),
            )

    async def assert_timeout_before_release() -> None:
        """Verify the timeout finishes while output and close remain gated."""
        run_task = asyncio.create_task(run_subject())
        writer_started = await asyncio.to_thread(
            destination.child_started.wait,
            4.0,
        )
        assert writer_started, "the worker must enter the child-output write"
        with pytest.raises(TimeoutExpired):
            await asyncio.wait_for(run_task, timeout=4.0)
        assert not destination.close_started.is_set(), (
            "session close must wait for the active destination write"
        )

    try:
        asyncio.run(assert_timeout_before_release())
    finally:
        destination.release_child.set()
        assert destination.close_finished.wait(timeout=2.0), (
            "the serialized session finalizer must run after the writer is released"
        )

    assert destination.messages == ("OPEN\n", "payload\n", "CLOSE\n")
