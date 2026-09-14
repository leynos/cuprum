"""Public-boundary coverage for aggregate stream-operation observation."""

from __future__ import annotations

from cuprum import ScopeConfig, scoped, sh
from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationOutcome,
)
from cuprum.stream_observation import observe_stream_operation
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
