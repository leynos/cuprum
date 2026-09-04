"""Executor-hop fixtures shared by the tracing behaviour scenarios."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import types
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.pump_span_observation import observe_pump_span

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.tracing_memory import InMemoryTracer


class _Guard:
    """Minimal guard that records descriptor restoration."""

    def __init__(self, events: list[str]) -> None:
        """Keep the shared ordering record."""
        self.events = events

    def restore(self) -> None:
        """Record that the task restored descriptor state."""
        self.events.append("restored")


def _install_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Install the local worker double at the optional-native seam."""
    module = types.ModuleType("cuprum._streams_rs")
    module.__dict__["rust_pump_stream"] = pump
    monkeypatch.setitem(sys.modules, "cuprum._streams_rs", module)


async def _run(
    pump: cabc.Callable[[int, int], int],
    *,
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a native-pump boundary over owned descriptors."""
    _install_pump(monkeypatch, pump)
    reader_fd, writer_fd = os.pipe()
    try:
        state = _pipeline_streams._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_stream_fds._BlockingModeGuard",
                _Guard(events),
            ),
            resume_reader=None,
        )
        await _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def run_successful_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a successful hop through its real executor callback."""

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Return the fixed transferred-byte total."""
        del reader_fd, writer_fd
        return 29

    with observe_pump_span(tracer):
        asyncio.run(_run(pump, events=[], monkeypatch=monkeypatch))


def run_cancelled_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Cancel a live worker and return its descriptor-ownership ordering."""
    events: list[str] = []
    started = threading.Event()
    release = threading.Event()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Wait until cancellation permits the worker to return."""
        del reader_fd, writer_fd
        started.set()
        if not release.wait(5.0):
            pytest.fail("harness did not release the native worker")
        events.append("worker_returned")
        return 0

    async def run_cancelled() -> None:
        """Drive cancellation after the worker has begun its transfer."""
        _install_pump(monkeypatch, pump)
        reader_fd, writer_fd = os.pipe()
        try:
            state = _pipeline_streams._RustPumpState(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                blocking_mode_guard=typ.cast(
                    "_pipeline_stream_fds._BlockingModeGuard",
                    _Guard(events),
                ),
                resume_reader=None,
            )
            task = asyncio.create_task(
                _pipeline_streams._run_rust_pump_with_blocking_fds(state=state),
            )
            if not await asyncio.to_thread(started.wait, 5.0):
                pytest.fail("worker did not start")
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            for fd in (reader_fd, writer_fd):
                with contextlib.suppress(OSError):
                    os.close(fd)

    with observe_pump_span(tracer):
        asyncio.run(run_cancelled())
    return events
