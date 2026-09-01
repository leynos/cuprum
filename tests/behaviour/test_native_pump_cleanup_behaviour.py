"""Public pipeline cancellation coverage for native-pump cleanup telemetry."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import threading
import typing as typ

import pytest

from cuprum import (
    ECHO,
    ScopeConfig,
    _pipeline_stream_fds,
    _pipeline_streams,
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
from cuprum.adapters.metrics_adapter import InMemoryMetrics
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_CLEANUP_DURATION_SECONDS,
    RUST_PUMP_CLEANUP_TOTAL,
    PumpMetricsHook,
)
from cuprum.pump_observation import observe_pump
from cuprum.unittests._rust_pump_test_helpers import install_fake_pump
from tests.helpers.catalogue import combine_programs_into_catalogue, python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.program import Program
    from cuprum.pump_events import PumpEvent
    from cuprum.sh import Pipeline


@dataclasses.dataclass(slots=True)
class _CleanupScenario:
    """State and observability captured during one public pipeline cancellation."""

    pipeline: Pipeline
    allowlist: frozenset[Program]
    worker_started: threading.Event = dataclasses.field(default_factory=threading.Event)
    worker_released: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    release_worker: threading.Event = dataclasses.field(default_factory=threading.Event)
    cleanup_started: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    cleanup_events: list[PumpEvent] = dataclasses.field(default_factory=list)
    restores: list[bool] = dataclasses.field(default_factory=list)
    writer_closes: list[bool] = dataclasses.field(default_factory=list)
    metrics: InMemoryMetrics = dataclasses.field(default_factory=InMemoryMetrics)

    def record_cleanup_event(self, event: PumpEvent) -> None:
        """Record cleanup events and signal when cancellation begins waiting."""
        self.cleanup_events.append(event)
        if event.phase == "cleanup_started":
            self.cleanup_started.set()


def _make_cleanup_scenario() -> _CleanupScenario:
    """Create a two-stage pipeline whose pump remains active until cancelled."""
    _, python_program = python_catalogue()
    catalogue = combine_programs_into_catalogue(
        ECHO,
        python_program,
        project_name="native-pump-cleanup-behaviour",
    )
    echo = sh.make(ECHO, catalogue=catalogue)
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = echo("-n", "payload") | python("-c", "import sys; sys.stdin.read()")
    return _CleanupScenario(pipeline, frozenset((ECHO, python_program)))


async def _cancel_public_pipeline(
    scenario: _CleanupScenario,
) -> None:
    """Cancel ``Pipeline.run`` only after its fake native worker starts."""
    task = asyncio.create_task(scenario.pipeline.run())
    try:
        started = await asyncio.to_thread(scenario.worker_started.wait, 5.0)
        assert started, "the fake native worker must start before cancellation"
        task.cancel()
        cleanup_waiting = await asyncio.to_thread(scenario.cleanup_started.wait, 5.0)
        assert cleanup_waiting, "cancellation must begin waiting for native cleanup"
        assert [event.phase for event in scenario.cleanup_events] == [
            "cleanup_started"
        ], "cleanup completion must wait for native worker descriptor ownership"
        assert scenario.restores == [], (
            "descriptor state must not restore before worker release"
        )
        assert scenario.writer_closes == [], (
            "duplicate writer must not close before worker release"
        )

        scenario.release_worker.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        scenario.release_worker.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert scenario.worker_released.is_set(), (
            "the native worker must release ownership"
        )


def _install_blocked_native_pump(
    monkeypatch: pytest.MonkeyPatch,
    scenario: _CleanupScenario,
) -> None:
    """Install worker and descriptor seams that enforce native ownership order."""
    original_restore = _pipeline_stream_fds._BlockingModeGuard.restore
    original_close = _pipeline_streams._close_rust_writer_fd

    def blocked_native_pump(reader_fd: int, writer_fd: int) -> int:
        """Retain worker descriptor ownership until the test releases it."""
        del reader_fd, writer_fd
        scenario.worker_started.set()
        released = scenario.release_worker.wait(timeout=5.0)
        assert released, "the test must release the fake native worker"
        scenario.worker_released.set()
        return 0

    def record_restore(guard: _pipeline_stream_fds._BlockingModeGuard) -> None:
        """Require descriptor restoration to follow native worker release."""
        assert scenario.worker_released.is_set(), (
            "descriptor state must not restore while the native worker owns it"
        )
        scenario.restores.append(True)
        original_restore(guard)

    def record_writer_close(writer_fd: int) -> None:
        """Require duplicate writer cleanup to follow native worker release."""
        assert scenario.worker_released.is_set(), (
            "duplicate writer must not close while the native worker owns it"
        )
        scenario.writer_closes.append(True)
        original_close(writer_fd)

    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard, "restore", record_restore
    )
    monkeypatch.setattr(_pipeline_streams, "_close_rust_writer_fd", record_writer_close)
    install_fake_pump(monkeypatch, blocked_native_pump)


@contextlib.contextmanager
def _force_rust_pump_path(
    monkeypatch: pytest.MonkeyPatch,
) -> cabc.Iterator[None]:
    """Supply owned descriptors through the supported stream-dispatch test seam."""
    reader_fd, writer_fd = os.pipe()

    def extract_raw_fd(
        stream: asyncio.StreamReader | asyncio.StreamWriter | None,
    ) -> int | None:
        """Return the owned descriptor matching the stream role under test."""
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


def _assert_cleanup_telemetry(scenario: _CleanupScenario) -> None:
    """Assert public cancellation emitted the complete cleanup telemetry contract."""
    assert [event.phase for event in scenario.cleanup_events] == [
        "cleanup_started",
        "cleanup_completed",
    ], (
        "cancellation must emit one ordered cleanup lifecycle, found "
        f"{scenario.cleanup_events}"
    )
    assert scenario.cleanup_events[0].duration_s is None, (
        "cleanup start must not carry a completion duration"
    )
    completion_duration = scenario.cleanup_events[1].duration_s
    assert completion_duration is not None, (
        "cleanup completion must carry a monotonic duration"
    )
    assert completion_duration >= 0.0, (
        "cleanup completion must carry a non-negative duration, found "
        f"{completion_duration!r}"
    )
    assert scenario.metrics.counters == {RUST_PUMP_CLEANUP_TOTAL: 1.0}, (
        "cleanup completion must increment its metric once, found "
        f"{scenario.metrics.counters}"
    )
    assert scenario.metrics.histograms == {
        RUST_PUMP_CLEANUP_DURATION_SECONDS: [completion_duration]
    }, (
        "cleanup completion must record one matching duration observation, found "
        f"{scenario.metrics.histograms}"
    )
    assert scenario.restores == [True], (
        f"descriptor restoration must occur once, found {scenario.restores}"
    )
    assert scenario.writer_closes == [True], (
        f"duplicate writer cleanup must occur once, found {scenario.writer_closes}"
    )


def test_cancelled_pipeline_reports_native_cleanup_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public pipeline cancellation emits one ordered native-cleanup lifecycle."""
    scenario = _make_cleanup_scenario()
    _install_blocked_native_pump(monkeypatch, scenario)
    with (
        _force_rust_pump_path(monkeypatch),
        scoped(ScopeConfig(allowlist=scenario.allowlist)),
        observe_pump(scenario.record_cleanup_event),
        observe_pump(PumpMetricsHook(scenario.metrics)),
    ):
        asyncio.run(_cancel_public_pipeline(scenario))

    _assert_cleanup_telemetry(scenario)
