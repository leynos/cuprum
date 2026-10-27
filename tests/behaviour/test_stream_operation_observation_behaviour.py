"""Public-boundary coverage for aggregate stream-operation observation."""

from __future__ import annotations

import inspect
import logging
import typing as typ

if typ.TYPE_CHECKING:
    import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationOutcome,
)
from cuprum.stream_observation import observe_stream_operation
from cuprum.unittests._hook_disposal_test_helpers import (
    _started_coroutine_raising,
    hook_returning,
)
from tests.helpers.catalogue import python_catalogue


def test_safe_command_emits_aggregate_drain_observation() -> None:
    """An allowlisted command reports its completed stdout drain."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)
    payload = b"stream-operation-behaviour"
    events: list[StreamOperationEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset((python_program,)))),
        observe_stream_operation(events.append),
    ):
        result = command(
            "-c",
            f"import sys; sys.stdout.buffer.write({payload!r})",
        ).run_sync()

    assert result.ok, "the public command must succeed"
    assert result.stdout == payload.decode(), "the public result must preserve stdout"
    assert events, "completed command streams must be observed"
    for event in events:
        assert event.operation is StreamOperation.DRAIN, (
            "command observations must identify draining"
        )
        assert event.outcome is StreamOperationOutcome.EOF, (
            "command drains must reach EOF"
        )
        assert event.duration_s >= 0, "event duration must be monotonic"

    payload_events = [event for event in events if event.bytes_consumed == len(payload)]
    assert len(payload_events) == 1, "one observed drain must contain stdout"
    payload_event = payload_events[0]
    assert payload_event.read_operations >= 2, (
        "the non-empty stdout drain must include its terminal EOF read"
    )


def test_coroutine_disposal_failure_preserves_public_command_and_later_hooks(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bad hook cannot fail a command or prevent later stream observers."""
    caplog.set_level(logging.WARNING, logger="cuprum.stream_observation")
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)
    payload = b"stream-operation-disposal-behaviour"
    coroutine = _started_coroutine_raising(RuntimeError("close failed"))
    received: list[StreamOperationEvent] = []
    failing_events: list[StreamOperationEvent] = []
    returning_hook = hook_returning(coroutine)

    def failing_hook(event: StreamOperationEvent) -> None:
        """Record the event before returning the coroutine that fails to close."""
        failing_events.append(event)
        return returning_hook(event)

    with (
        scoped(ScopeConfig(allowlist=frozenset((python_program,)))),
        observe_stream_operation(failing_hook),
        observe_stream_operation(received.append),
    ):
        result = command(
            "-c",
            f"import sys; sys.stdout.buffer.write({payload!r})",
        ).run_sync()

    assert result.ok, "disposal failure must not fail the public command"
    assert result.stdout == payload.decode(), "the command must preserve its output"
    assert failing_events, "the failing hook must receive a completed operation event"
    assert any(event is failing_events[0] for event in received), (
        "the later hook must receive the event whose disposal failed"
    )
    disposal_records = [
        record
        for record in caplog.records
        if record.__dict__.get("cuprum_action")
        == "stream_operation_observer_disposal_failed"
    ]
    assert len(disposal_records) == 1, (
        "the started coroutine's close failure must be reported once"
    )
    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED, (
        "the returned coroutine must be closed"
    )
