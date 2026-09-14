"""Integration tests for capture EOF grace observability.

The production scheduler budget is deliberately private and time-bounded. These
tests replace only its waiter seam, so they exercise the public timeout path
without waiting for wall-clock time or exposing a configuration option.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import Program, TimeoutExpired, sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.context import ScopeConfig, scoped
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import child_argv, python_interpreter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._streams import _StreamConfig
    from cuprum.events import ExecEvent


@pytest.fixture
def readers_that_expire_grace_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Make the public capture timeout path reach a deterministic grace expiry."""

    async def consume_forever(
        _stream: asyncio.StreamReader | None,
        _config: _StreamConfig,
        *,
        on_line: cabc.Callable[[str], None] | None = None,
        read_size: int = 4096,
    ) -> str | None:
        """Stand in for a reader that cannot observe EOF before cancellation."""
        del on_line, read_size
        await asyncio.Event().wait()
        return None

    async def expire_immediately(
        _consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    ) -> None:
        """Close the test-only grace window without elapsed wall-clock time."""

    monkeypatch.setattr(
        "cuprum._subprocess_execution._consume_stream",
        consume_forever,
    )
    monkeypatch.setattr("cuprum._subprocess_wait._await_eof_grace", expire_immediately)


def _capture_timeout_command(tmp_path: Path) -> tuple[sh.SafeCmd, ScopeConfig]:
    """Build a process that reaches the public capture timeout path."""
    catalogue = python_catalogue()[0]
    command = sh.make(Program(python_interpreter()), catalogue=catalogue)(
        *child_argv(tmp_path / "ready")
    )
    return command, ScopeConfig(allowlist=catalogue.allowlist)


def test_capture_grace_expiry_emits_one_correlated_event(
    tmp_path: Path,
    readers_that_expire_grace_immediately: None,
) -> None:
    """Public capture expiry emits one payload-free correlated event."""
    command, scope_config = _capture_timeout_command(tmp_path)
    events: list[ExecEvent] = []

    def record(event: ExecEvent) -> None:
        """Retain every lifecycle event for one timed-out execution."""
        events.append(event)

    with (
        scoped(scope_config),
        sh.observe(record),
        pytest.raises(TimeoutExpired),
    ):
        command.run_sync(timeout=0, output=RunOutputOptions(capture=True))

    expiries = [event for event in events if event.phase == "capture_eof_grace_expired"]
    assert len(expiries) == 1, f"expected one grace event, got {events!r}"
    expiry = expiries[0]
    starts = [event for event in events if event.phase == "start"]
    assert len(starts) == 1, f"expected one start event, got {events!r}"
    assert expiry.exec_id == starts[0].exec_id, (
        "the grace event must retain the execution correlation token"
    )
    assert expiry.pid == starts[0].pid, "the grace event must retain the process pid"
    assert (
        expiry.operation,
        expiry.eof_grace_s,
        expiry.pending_readers,
        expiry.line,
        expiry.note,
        expiry.error_type,
    ) == ("drain", 0.25, 2, None, None, None), (
        "grace expiry must expose only bounded drain diagnostics, never capture"
    )


def test_failing_grace_observer_does_not_mask_timeout(
    tmp_path: Path,
    readers_that_expire_grace_immediately: None,
) -> None:
    """A grace-event hook failure leaves the public timeout exception primary."""
    command, scope_config = _capture_timeout_command(tmp_path)

    def fail_on_grace(event: ExecEvent) -> None:
        """Simulate a broken observer only for the grace expiry event."""
        if event.phase == "capture_eof_grace_expired":
            msg = "grace observer exploded"
            raise RuntimeError(msg)

    with (
        scoped(scope_config),
        sh.observe(fail_on_grace),
        pytest.raises(TimeoutExpired),
    ):
        command.run_sync(timeout=0, output=RunOutputOptions(capture=True))


def test_readers_reaching_eof_emit_no_grace_event_or_metric(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readers that finish inside grace leave no expiry telemetry behind."""

    async def wait_for_readers(
        consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
    ) -> None:
        """Wait for the closed process pipes to deliver EOF to both readers."""
        await asyncio.gather(*consumers)

    monkeypatch.setattr("cuprum._subprocess_wait._await_eof_grace", wait_for_readers)
    command, scope_config = _capture_timeout_command(tmp_path)
    metrics = InMemoryMetrics()
    events: list[ExecEvent] = []
    metric_hook = MetricsHook(metrics)

    def record_and_measure(event: ExecEvent) -> None:
        """Retain each event after projecting it through the metrics adapter."""
        metric_hook(event)
        events.append(event)

    with (
        scoped(scope_config),
        sh.observe(record_and_measure),
        pytest.raises(TimeoutExpired),
    ):
        command.run_sync(timeout=0, output=RunOutputOptions(capture=True))

    assert "capture_eof_grace_expired" not in [event.phase for event in events], (
        "readers that reached EOF inside grace must not emit an expiry event"
    )
    assert "cuprum_capture_eof_grace_expired_total" not in metrics.counters, (
        "without a grace expiry event the adapter must not increment its counter"
    )


def test_non_capturing_stream_drain_emits_no_grace_event_or_metric(
    tmp_path: Path,
) -> None:
    """Non-capturing streamed cleanup skips grace telemetry entirely."""
    command, scope_config = _capture_timeout_command(tmp_path)
    metrics = InMemoryMetrics()
    events: list[ExecEvent] = []
    metric_hook = MetricsHook(metrics)

    def record_and_measure(event: ExecEvent) -> None:
        """Retain each non-capturing event after metric projection."""
        metric_hook(event)
        events.append(event)

    with (
        scoped(scope_config),
        sh.observe(record_and_measure),
        pytest.raises(TimeoutExpired),
    ):
        command.run_sync(timeout=0, output=RunOutputOptions(echo=True))

    assert "capture_eof_grace_expired" not in [event.phase for event in events], (
        "a non-capturing drain must not emit capture grace expiry telemetry"
    )
    assert "cuprum_capture_eof_grace_expired_total" not in metrics.counters, (
        "a non-capturing drain must not increment the grace-expiry counter"
    )
