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


def _group_payload(**overrides: int | float) -> dict[str, int | float]:
    """Return a valid serialized group payload with per-field overrides."""
    payload: dict[str, int | float] = {
        "bytes_consumed": 4,
        "read_operations": 2,
        "operation_count": 1,
        "duration_seconds": 0.25,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
        pytest.param("stream_drain", id="string"),
        pytest.param(7, id="integer"),
        pytest.param({}, id="mapping-without-groups"),
        pytest.param({"groups": []}, id="non-mapping-groups"),
        pytest.param({"groups": None}, id="null-groups"),
        pytest.param({"stream_drain": {"eof": _group_payload()}}, id="no-groups-key"),
    ],
)
def test_snapshot_rejects_payload_without_a_group_mapping(payload: object) -> None:
    """Payloads that are not mappings of groups yield an empty snapshot."""
    snapshot = StreamTelemetrySnapshot.from_dict(payload)

    assert snapshot == StreamTelemetrySnapshot.empty()


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        pytest.param(
            {"unknown_operation": {"eof": _group_payload()}},
            set(),
            id="operation",
        ),
        pytest.param(
            {"stream_drain": {"unknown_outcome": _group_payload()}},
            set(),
            id="outcome",
        ),
        pytest.param(
            {
                "unknown_operation": {"unknown_outcome": _group_payload()},
                "stream_drain": {"eof": _group_payload()},
            },
            {(StreamOperation.DRAIN, StreamOperationOutcome.EOF)},
            id="mixed-labels",
        ),
    ],
)
def test_snapshot_ignores_unknown_operation_and_outcome_labels(
    groups: dict[str, object],
    expected: set[tuple[StreamOperation, StreamOperationOutcome]],
) -> None:
    """Only the closed operation and outcome vocabularies contribute groups."""
    snapshot = StreamTelemetrySnapshot.from_dict({"groups": groups})

    assert set(snapshot.groups) == expected, f"unknown labels must not group: {groups}"


def test_snapshot_keeps_valid_groups_alongside_invalid_entries() -> None:
    """Valid groups survive unknown labels, invalid values, and non-mappings."""
    snapshot = StreamTelemetrySnapshot.from_dict({
        "groups": {
            "stream_drain": {
                "eof": _group_payload(
                    bytes_consumed=8,
                    read_operations=3,
                    operation_count=2,
                    duration_seconds=1.5,
                ),
                "cancelled": _group_payload(duration_seconds=-1.0),
                "failed": "not-a-mapping",
                "unknown_outcome": _group_payload(),
            },
            "pipeline_transfer": [],
            "unknown_operation": {"eof": _group_payload()},
        },
    })

    assert snapshot.groups == {
        (StreamOperation.DRAIN, StreamOperationOutcome.EOF): StreamTelemetryGroup(
            bytes_consumed=8,
            read_operations=3,
            operation_count=2,
            duration_seconds=1.5,
        )
    }, "only the recognized, valid group may survive"
    assert (
        snapshot.totals
        == snapshot.groups[StreamOperation.DRAIN, StreamOperationOutcome.EOF]
    ), "totals must sum exactly the accepted groups"


def test_snapshot_recalculates_totals_from_accepted_groups() -> None:
    """Serialized totals are ignored in favour of the accepted groups."""
    untrusted = 10**9
    snapshot = StreamTelemetrySnapshot.from_dict({
        "groups": {
            "stream_drain": {
                "eof": _group_payload(
                    bytes_consumed=5,
                    read_operations=3,
                    operation_count=1,
                    duration_seconds=0.5,
                )
            },
            "pipeline_transfer": {
                "cancelled": _group_payload(
                    bytes_consumed=7,
                    read_operations=4,
                    operation_count=2,
                    duration_seconds=1.25,
                )
            },
        },
        "totals": {
            "bytes_consumed": untrusted,
            "read_operations": untrusted,
            "operation_count": untrusted,
            "duration_seconds": float(untrusted),
        },
    })

    assert snapshot.totals == StreamTelemetryGroup(
        bytes_consumed=12,
        read_operations=7,
        operation_count=3,
        duration_seconds=1.75,
    ), "totals must be summed from the accepted groups"
    assert snapshot.as_dict()["totals"] == snapshot.totals.as_dict(), (
        "the serialized totals must describe the accumulated groups"
    )


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
