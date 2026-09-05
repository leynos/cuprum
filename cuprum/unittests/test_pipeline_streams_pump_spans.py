"""Executor-seam tests for opt-in Rust-pump hop spans."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import sys
import threading
import types
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.pump_span_events import (
    PUMP_HOP_BUFFER_SIZE_ATTRIBUTE,
    PUMP_HOP_OPERATION_ATTRIBUTE,
    PUMP_HOP_OUTCOME_ATTRIBUTE,
    PUMP_HOP_SPAN_NAME,
    PUMP_HOP_TOTAL_BYTES_ATTRIBUTE,
    PumpHopOutcome,
)
from cuprum.pump_span_observation import observe_pump_span
from cuprum.unittests._rust_pump_test_helpers import DECLINE_PATHS

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _RecordingGuard:
    """Blocking-mode guard double that records restoration ordering."""

    def __init__(self, events: list[str]) -> None:
        """Store the shared ordering log."""
        self.events = events

    def restore(self) -> None:
        """Record restoration after worker settlement."""
        self.events.append("restored")


@dc.dataclass
class _Transfer:
    """Synchronization state for a cancellation transfer."""

    events: list[str]
    started: threading.Event
    release: threading.Event


def _install_fake_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Install a local Rust-pump double without loading the extension."""
    module = types.ModuleType("cuprum._streams_rs")
    module.__dict__["rust_pump_stream"] = pump
    monkeypatch.setitem(sys.modules, "cuprum._streams_rs", module)


async def _run_pump(
    pump: cabc.Callable[[int, int], int],
    *,
    events: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a fake Rust pump over owned descriptors."""
    _install_fake_pump(monkeypatch, pump)
    reader_fd, writer_fd = os.pipe()
    try:
        state = _pipeline_streams._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_stream_fds._BlockingModeGuard",
                _RecordingGuard(events),
            ),
            resume_reader=None,
        )
        await _pipeline_streams._run_rust_pump_with_blocking_fds(state=state)
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


def test_successful_executor_hop_opens_and_ends_one_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful fast-path transfer records its bounded completion facts."""
    tracer = InMemoryTracer()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Return the fixed byte count the executor callback must record."""
        del reader_fd, writer_fd
        return 23

    with observe_pump_span(tracer):
        asyncio.run(_run_pump(pump, events=[], monkeypatch=monkeypatch))

    assert len(tracer.spans) == 1, f"expected one hop span, found {tracer.spans}"
    span = tracer.spans[0]
    assert span.name == PUMP_HOP_SPAN_NAME, f"unexpected span name {span.name!r}"
    assert span.ended is True, "successful hop span must end"
    assert span.status_ok is True, "successful hop span must be marked ok"
    assert span.attributes == {
        PUMP_HOP_OPERATION_ATTRIBUTE: "rust_pump",
        PUMP_HOP_BUFFER_SIZE_ATTRIBUTE: 65_536,
        PUMP_HOP_OUTCOME_ATTRIBUTE: "succeeded",
        PUMP_HOP_TOTAL_BYTES_ATTRIBUTE: 23,
    }, f"unexpected bounded span attributes {span.attributes}"


@pytest.mark.parametrize(
    "trigger",
    [trigger for _path_id, trigger, _reason in DECLINE_PATHS],
    ids=[path_id for path_id, _trigger, _reason in DECLINE_PATHS],
)
def test_declined_paths_open_no_executor_hop_span(
    monkeypatch: pytest.MonkeyPatch,
    trigger: cabc.Callable[[pytest.MonkeyPatch], None],
) -> None:
    """Fast-path declines must not be represented as executor hop spans."""
    tracer = InMemoryTracer()

    with observe_pump_span(tracer):
        trigger(monkeypatch)

    assert tracer.spans == [], f"declined path must not open a span: {tracer.spans}"


async def _cancel_pump(
    transfer: _Transfer,
    pump: cabc.Callable[[int, int], int],
    *,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancel a live worker, then release it so callback cleanup can finish."""
    _install_fake_pump(monkeypatch, pump)
    reader_fd, writer_fd = os.pipe()
    try:
        state = _pipeline_streams._RustPumpState(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            blocking_mode_guard=typ.cast(
                "_pipeline_stream_fds._BlockingModeGuard",
                _RecordingGuard(transfer.events),
            ),
            resume_reader=None,
        )
        task = asyncio.create_task(
            _pipeline_streams._run_rust_pump_with_blocking_fds(state=state),
        )
        assert await asyncio.to_thread(transfer.started.wait, 5.0), (
            "the worker did not start, so the task was not cancelled mid-transfer"
        )
        task.cancel()
        await asyncio.sleep(0)
        transfer.release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)


@pytest.mark.parametrize(
    ("should_fail", "expected_outcome"),
    [(False, "cancelled"), (True, "failed_after_cancel")],
    ids=("clean-worker", "failing-worker"),
)
def test_cancelled_executor_hop_ends_with_expected_outcome(
    *,
    should_fail: bool,
    expected_outcome: PumpHopOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelled hops retain worker ownership until the expected outcome is set."""
    transfer = _Transfer([], threading.Event(), threading.Event())
    tracer = InMemoryTracer()

    def pump(reader_fd: int, writer_fd: int) -> int:
        """Wait for cancellation, then return or fail as configured."""
        del reader_fd, writer_fd
        transfer.started.set()
        assert transfer.release.wait(5.0), "harness did not release the worker"
        transfer.events.append("worker_returned")
        if should_fail:
            msg = "worker failed after cancellation"
            raise OSError(msg)
        return 0

    with observe_pump_span(tracer):
        asyncio.run(_cancel_pump(transfer, pump, monkeypatch=monkeypatch))

    span = tracer.spans[0]
    assert span.ended is True, "cancelled hop span must end"
    assert span.attributes[PUMP_HOP_OUTCOME_ATTRIBUTE] == expected_outcome, (
        f"unexpected cancellation outcome {span.attributes}"
    )
    assert span.status_ok is not True, "cancelled hop must not be marked ok"
    assert transfer.events.index("worker_returned") < transfer.events.index(
        "restored"
    ), f"worker must return before descriptor restore: {transfer.events}"
