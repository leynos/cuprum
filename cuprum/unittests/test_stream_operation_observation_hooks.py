"""Unit tests for stream-operation observer registration and completion edges."""

from __future__ import annotations

import asyncio
import inspect
import io
import logging
import typing as typ

import pytest

from cuprum._streams import _drain, _StreamConfig
from cuprum._streams_pump import _pump_stream
from cuprum.stream_events import (
    StreamOperation,
    StreamOperationHook,
    StreamOperationOutcome,
)
from cuprum.stream_observation import (
    _complete_stream_operation,
    _start_stream_operation,
    current_stream_operation_hooks,
    observe_stream_operation,
)


class _CancelledReader:
    """Reader double that delivers cancellation from its first read."""

    async def read(self, _: int) -> bytes:
        """Raise the cancellation expected by the caller."""
        raise asyncio.CancelledError


class _PayloadReader:
    """Reader double that returns one payload followed by EOF."""

    def __init__(self, payload: bytes) -> None:
        """Store the payload returned by the first read."""
        self._payload = payload

    async def read(self, _: int) -> bytes:
        """Return the payload once and EOF thereafter."""
        payload, self._payload = self._payload, b""
        return payload


class _Writer:
    """Minimal writer double accepted by the pipeline pump."""

    def write(self, _: bytes) -> None:
        """Accept a written chunk."""

    async def drain(self) -> None:
        """Accept backpressure completion."""

    def write_eof(self) -> None:
        """Accept writer EOF."""

    def close(self) -> None:
        """Accept writer close."""

    async def wait_closed(self) -> None:
        """Accept writer close completion."""


def _drain_config(*, capture_output: bool = True) -> _StreamConfig:
    """Build a minimal stream configuration for observation tests."""
    return _StreamConfig(
        capture_output=capture_output,
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
    )


def test_non_none_observer_return_is_warned_and_does_not_change_drain(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A synchronous hook return is reported but cannot alter stream success."""
    caplog.set_level(logging.WARNING, logger="cuprum.stream_observation")

    returning_hook = typ.cast("StreamOperationHook", lambda _event: "ignored")
    with observe_stream_operation(returning_hook):
        captured = asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _PayloadReader(b"payload")),
                _drain_config(),
            )
        )

    assert captured == "payload", "an observer return must not change draining"
    assert [record.__dict__.get("cuprum_action") for record in caplog.records] == [
        "stream_operation_observer_returned_value"
    ], "a non-None observer return must produce its bounded warning"


def test_coroutine_observer_return_is_closed() -> None:
    """An accidental awaitable is closed rather than leaked or awaited."""

    async def unexpected_coroutine() -> None:
        """Stand in for an accidentally asynchronous observer result."""

    coroutine = unexpected_coroutine()
    returning_hook = typ.cast("StreamOperationHook", lambda _event: coroutine)
    with observe_stream_operation(returning_hook):
        asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _PayloadReader(b"payload")),
                _drain_config(),
            )
        )

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED, (
        "an accidental coroutine observer result must be closed"
    )


def test_out_of_order_detach_removes_each_registered_hook() -> None:
    """Non-LIFO detachment must not restore either stale hook registration."""
    seen_a: list[object] = []
    seen_b: list[object] = []
    registration_a = observe_stream_operation(seen_a.append)
    registration_b = observe_stream_operation(seen_b.append)

    registration_a.detach()
    registration_b.detach()

    assert current_stream_operation_hooks() == (), (
        "out-of-order detach must leave no stale registered hooks"
    )
    asyncio.run(
        _drain(
            typ.cast("asyncio.StreamReader", _PayloadReader(b"payload")),
            _drain_config(),
        )
    )
    assert seen_a == [], "operations after both detaches must not invoke hook A"
    assert seen_b == [], "operations after both detaches must not invoke hook B"


def test_measurement_uses_its_injected_monotonic_clock() -> None:
    """The measurement uses the registration boundary's supplied clock."""
    seen = []
    timestamps = iter((10.0, 12.5))

    with observe_stream_operation(seen.append):
        measurement = _start_stream_operation(
            StreamOperation.DRAIN,
            monotonic_clock=lambda: next(timestamps),
        )
        _complete_stream_operation(measurement, StreamOperationOutcome.EOF)

    assert seen[0].duration_s == pytest.approx(2.5), (
        "the event duration must come from the injected monotonic clock"
    )


def test_drain_completes_cancellation_before_reraising() -> None:
    """A cancelled non-capturing drain publishes its terminal outcome first."""
    seen = []

    with observe_stream_operation(seen.append), pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _drain(
                typ.cast("asyncio.StreamReader", _CancelledReader()),
                _drain_config(capture_output=False),
            )
        )

    assert [event.outcome for event in seen] == [StreamOperationOutcome.CANCELLED], (
        "a cancelled drain must publish exactly one CANCELLED event"
    )


def test_pump_completes_cancellation_after_writer_cleanup() -> None:
    """A cancelled relay publishes its terminal outcome while closing its writer."""
    seen = []

    with observe_stream_operation(seen.append), pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _pump_stream(
                typ.cast("asyncio.StreamReader", _CancelledReader()),
                typ.cast("asyncio.StreamWriter", _Writer()),
            )
        )

    assert [event.outcome for event in seen] == [StreamOperationOutcome.CANCELLED], (
        "a cancelled relay must publish exactly one CANCELLED event"
    )
