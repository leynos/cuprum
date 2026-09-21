"""Tests for bounded aggregate tee-profile stream telemetry."""

from __future__ import annotations

import typing as typ

import pytest

from benchmarks._tee_profile_stream_telemetry import (
    StreamTelemetryAccumulator,
    StreamTelemetryGroup,
    StreamTelemetrySnapshot,
)
from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationOutcome,
)


def test_snapshot_copies_its_group_mapping() -> None:
    """Snapshots do not observe mappings mutated after their construction."""
    original_groups: dict[
        tuple[StreamOperation, StreamOperationOutcome], StreamTelemetryGroup
    ] = {}
    snapshot = StreamTelemetrySnapshot(
        groups=original_groups,
        totals=StreamTelemetryGroup(),
    )

    original_groups[StreamOperation.DRAIN, StreamOperationOutcome.EOF] = (
        StreamTelemetryGroup(operation_count=1)
    )

    assert not snapshot.groups, "snapshot must keep a defensive group copy"


@pytest.mark.parametrize(
    "invalid_group",
    [
        pytest.param({"bytes_consumed": -1}, id="negative-bytes"),
        pytest.param({"read_operations": True}, id="boolean-read-operations"),
        pytest.param({"operation_count": -1}, id="negative-operation-count"),
        pytest.param({"duration_seconds": -1.0}, id="negative-duration"),
        pytest.param({"duration_seconds": float("inf")}, id="infinite-duration"),
        pytest.param({"duration_seconds": float("nan")}, id="nan-duration"),
    ],
)
def test_snapshot_discards_invalid_serialized_group(
    invalid_group: dict[str, int | float | bool],
) -> None:
    """Serialized groups reject impossible counter and duration values."""
    group: dict[str, int | float | bool] = {
        "bytes_consumed": 1,
        "read_operations": 1,
        "operation_count": 1,
        "duration_seconds": 1.0,
    }
    group.update(invalid_group)

    snapshot = StreamTelemetrySnapshot.from_dict({
        "groups": {"stream_drain": {"eof": group}}
    })

    assert not snapshot.groups, f"invalid telemetry must be discarded: {group}"
    assert snapshot.totals == StreamTelemetryGroup()


def test_accumulator_discards_unknown_operation_and_outcome() -> None:
    """Accumulator groups only events using the closed operation vocabulary."""
    accumulator = StreamTelemetryAccumulator()
    accumulator(
        StreamOperationEvent(
            operation=typ.cast("StreamOperation", "unknown_operation"),
            outcome=typ.cast("StreamOperationOutcome", "unknown_outcome"),
            bytes_consumed=1,
            read_operations=1,
            duration_s=1.0,
        )
    )

    assert accumulator.snapshot() == StreamTelemetrySnapshot.empty()
