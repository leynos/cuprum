"""Executor-hop fixtures shared by the tracing behaviour scenarios."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import threading
import typing as typ

import pytest

from cuprum import (
    ECHO,
    ScopeConfig,
    _pipeline_stream_fds,
    _rust_backend,
    scoped,
    sh,
)
from cuprum._backend import (
    _check_rust_available,
    get_stream_backend,
    set_rust_availability_for_testing,
)
from cuprum._testing import (
    configure_pump_stream_dispatch_for_testing,
    reset_pump_stream_dispatch_for_testing,
)
from cuprum.pump_observation import observe_pump
from cuprum.pump_span_observation import observe_pump_span
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.tracing_memory import InMemorySpan, InMemoryTracer
    from cuprum.program import Program
    from cuprum.pump_events import PumpEvent
    from cuprum.sh import Pipeline


@dc.dataclass(slots=True)
class _CancelledHopScenario:
    """State captured while a public pipeline cancellation owns descriptors."""

    pipeline: Pipeline
    allowlist: frozenset[Program]
    events: list[str] = dc.field(default_factory=list)
    worker_started: threading.Event = dc.field(default_factory=threading.Event)
    worker_released: threading.Event = dc.field(default_factory=threading.Event)
    release_worker: threading.Event = dc.field(default_factory=threading.Event)
    cleanup_started: threading.Event = dc.field(default_factory=threading.Event)


class _SpanEndRecorder:
    """Forward a span while recording its terminal boundary."""

    def __init__(self, span: InMemorySpan, events: list[str]) -> None:
        """Store the wrapped span and its ordering log."""
        self._span = span
        self._events = events
        self._ended = False

    def set_attribute(self, key: str, value: object) -> None:
        """Forward an attribute update."""
        self._ensure_open()
        self._span.set_attribute(key, value)

    def add_event(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> None:
        """Forward an event update."""
        self._ensure_open()
        self._span.add_event(name, attributes)

    def set_status(self, *, ok: bool) -> None:
        """Forward a terminal status update."""
        self._ensure_open()
        self._span.set_status(ok=ok)

    def end(self) -> None:
        """End the wrapped span and record that terminal boundary."""
        self._ensure_open()
        self._span.end()
        self._ended = True
        self._events.append("span_ended")

    def _ensure_open(self) -> None:
        """Reject post-closure recording in the ordering double."""
        if self._ended:
            pytest.fail("the observed span must not receive records after ending")


class _SpanEndRecordingTracer:
    """Wrap an in-memory tracer to make span closure externally observable."""

    def __init__(self, tracer: InMemoryTracer, events: list[str]) -> None:
        """Store the inspected tracer and ordering log."""
        self._tracer = tracer
        self._events = events

    def start_span(
        self,
        name: str,
        attributes: cabc.Mapping[str, object] | None = None,
    ) -> _SpanEndRecorder:
        """Open a wrapped span on the inspected tracer."""
        return _SpanEndRecorder(self._tracer.start_span(name, attributes), self._events)


def run_successful_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a public multi-stage pipeline through the installed Rust backend."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")

    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="rust-pump-span-behaviour",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = echo("-n", "rust-pump-span-behaviour") | python(
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )

    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    try:
        with (
            observe_pump_span(tracer),
            scoped(
                ScopeConfig(allowlist=frozenset((ECHO, python_program))),
            ),
        ):
            result = pipeline.run_sync()
    finally:
        _check_rust_available.cache_clear()
        get_stream_backend.cache_clear()

    if result.stdout != "rust-pump-span-behaviour":
        pytest.fail("the public pipeline must transfer its stdout through the Rust hop")


def run_cancelled_hop(
    tracer: InMemoryTracer,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    """Cancel a public Rust pipeline and return its terminal ordering."""
    if not _rust_backend.is_available():
        pytest.skip("Rust extension is not installed")

    scenario = _make_cancelled_hop_scenario()
    _install_blocked_native_worker(monkeypatch, scenario)
    with (
        _force_rust_pump_path(monkeypatch),
        observe_pump_span(_SpanEndRecordingTracer(tracer, scenario.events)),
        observe_pump(_record_cleanup_start(scenario)),
        scoped(ScopeConfig(allowlist=scenario.allowlist)),
    ):
        asyncio.run(_cancel_public_pipeline(scenario))
    return scenario.events


def _make_cancelled_hop_scenario() -> _CancelledHopScenario:
    """Build the public two-stage pipeline used for cancellation coverage."""
    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="rust-pump-span-cancellation-behaviour",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = echo("-n", "payload") | python("-c", "import sys; sys.stdin.read()")
    return _CancelledHopScenario(pipeline, frozenset((ECHO, python_program)))


def _record_cleanup_start(
    scenario: _CancelledHopScenario,
) -> cabc.Callable[[PumpEvent], None]:
    """Build the pump-event observer that signals cancellation cleanup."""

    def record(event: PumpEvent) -> None:
        """Signal when cancellation starts waiting for the native worker."""
        if event.phase == "cleanup_started":
            scenario.cleanup_started.set()

    return record


async def _cancel_public_pipeline(scenario: _CancelledHopScenario) -> None:
    """Cancel the public pipeline after its native worker owns descriptors."""
    task = asyncio.create_task(scenario.pipeline.run())
    try:
        worker_started = await asyncio.to_thread(scenario.worker_started.wait, 5.0)
        if not worker_started:
            pytest.fail("the native worker must start before cancellation")
        task.cancel()
        cleanup_started = await asyncio.to_thread(scenario.cleanup_started.wait, 5.0)
        if not cleanup_started:
            pytest.fail("cancellation must begin native-worker cleanup")
        if scenario.worker_released.is_set():
            pytest.fail("cleanup must begin while the native worker owns descriptors")
        if "restored" in scenario.events:
            pytest.fail("descriptor restoration must wait for native worker settlement")
        scenario.release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        scenario.release_worker.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if not scenario.worker_released.is_set():
            pytest.fail("the native worker must return before cancellation finishes")


def _install_blocked_native_worker(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CancelledHopScenario,
) -> None:
    """Install the controlled native-worker and restoration seams."""
    original_restore = _pipeline_stream_fds._BlockingModeGuard.restore

    def blocked_native_worker(reader_fd: int, writer_fd: int) -> int:
        """Hold the Rust worker until cancellation has started cleanup."""
        del reader_fd
        scenario.worker_started.set()
        released = scenario.release_worker.wait(timeout=5.0)
        if not released:
            pytest.fail("the cancellation harness must release the native worker")
        os.close(writer_fd)
        scenario.events.append("worker_returned")
        scenario.worker_released.set()
        return 0

    def record_restore(guard: _pipeline_stream_fds._BlockingModeGuard) -> None:
        """Record restoration only after native descriptor ownership ends."""
        if not scenario.worker_released.is_set():
            pytest.fail(
                "descriptor restoration ran while the native worker owned descriptors"
            )
        scenario.events.append("restored")
        original_restore(guard)

    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard,
        "restore",
        record_restore,
    )
    install_fake_pump(monkeypatch, blocked_native_worker)


@contextlib.contextmanager
def _force_rust_pump_path(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Iterator[None]:
    """Force public stream dispatch through controlled Rust-worker descriptors."""
    reader_fd, writer_fd = os.pipe()

    def extract_raw_fd(
        stream: asyncio.StreamReader | asyncio.StreamWriter | None,
    ) -> int | None:
        """Return the owned descriptor for the stream role under test."""
        match stream:
            case asyncio.StreamReader():
                return reader_fd
            case asyncio.StreamWriter():
                return writer_fd
            case _:
                return None

    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    configure_pump_stream_dispatch_for_testing(raw_fd_extractor=extract_raw_fd)
    set_rust_availability_for_testing(is_available=True)
    _check_rust_available.cache_clear()
    get_stream_backend.cache_clear()
    try:
        yield
    finally:
        reset_pump_stream_dispatch_for_testing()
        set_rust_availability_for_testing(is_available=None)
        _check_rust_available.cache_clear()
        get_stream_backend.cache_clear()
        for fd in (reader_fd, writer_fd):
            with contextlib.suppress(OSError):
                os.close(fd)
