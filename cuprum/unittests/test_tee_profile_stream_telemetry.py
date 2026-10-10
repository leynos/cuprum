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
        pytest.param({"duration_seconds": 10**400}, id="overflowing-duration"),
        pytest.param({"duration_seconds": float("-inf")}, id="negative-infinite"),
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


def test_snapshot_keeps_counters_beyond_the_float_range() -> None:
    """Integer counters are boundaries, so values beyond binary64 survive."""
    huge = 10**400

    snapshot = StreamTelemetrySnapshot.from_dict({
        "groups": {"stream_drain": {"eof": _group_payload(bytes_consumed=huge)}}
    })

    assert snapshot.groups == {
        (StreamOperation.DRAIN, StreamOperationOutcome.EOF): StreamTelemetryGroup(
            bytes_consumed=huge,
            read_operations=2,
            operation_count=1,
            duration_seconds=0.25,
        )
    }, "byte and operation counters must not be narrowed to a float range"


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


def _event(
    operation: StreamOperation,
    outcome: StreamOperationOutcome,
    *,
    bytes_consumed: int,
    read_operations: int,
    duration_s: float,
) -> StreamOperationEvent:
    """Return one completed-operation event for accumulator coverage."""
    return StreamOperationEvent(
        operation=operation,
        outcome=outcome,
        bytes_consumed=bytes_consumed,
        read_operations=read_operations,
        duration_s=duration_s,
    )


def test_accumulator_merges_events_within_and_across_groups() -> None:
    """Repeated events accumulate into their own group, leaving others alone."""
    accumulator = StreamTelemetryAccumulator()
    drain_eof = (StreamOperation.DRAIN, StreamOperationOutcome.EOF)

    for event in (
        _event(*drain_eof, bytes_consumed=4, read_operations=2, duration_s=0.25),
        _event(*drain_eof, bytes_consumed=6, read_operations=3, duration_s=0.5),
        # A distinct outcome must stay separate from the same operation's EOF
        # group rather than being folded into it.
        _event(
            StreamOperation.DRAIN,
            StreamOperationOutcome.CANCELLED,
            bytes_consumed=1,
            read_operations=1,
            duration_s=0.125,
        ),
        # A distinct operation must stay separate from the drain group.
        _event(
            StreamOperation.PIPELINE_TRANSFER,
            StreamOperationOutcome.EOF,
            bytes_consumed=8,
            read_operations=4,
            duration_s=1.0,
        ),
    ):
        accumulator(event)

    snapshot = accumulator.snapshot()
    assert snapshot.groups == {
        drain_eof: StreamTelemetryGroup(
            bytes_consumed=10,
            read_operations=5,
            operation_count=2,
            duration_seconds=0.75,
        ),
        (StreamOperation.DRAIN, StreamOperationOutcome.CANCELLED): (
            StreamTelemetryGroup(
                bytes_consumed=1,
                read_operations=1,
                operation_count=1,
                duration_seconds=0.125,
            )
        ),
        (StreamOperation.PIPELINE_TRANSFER, StreamOperationOutcome.EOF): (
            StreamTelemetryGroup(
                bytes_consumed=8,
                read_operations=4,
                operation_count=1,
                duration_seconds=1.0,
            )
        ),
    }, "each group must accumulate independently of the others"
    assert snapshot.totals == StreamTelemetryGroup(
        bytes_consumed=19,
        read_operations=10,
        operation_count=4,
        duration_seconds=1.875,
    ), "totals must sum every accumulated group"
    assert snapshot.as_dict()["totals"] == snapshot.totals.as_dict(), (
        "the serialized totals must describe the accumulated groups"
    )


def test_accumulator_merges_snapshots_into_existing_groups() -> None:
    """Snapshot merging adds to the groups an accumulator already holds."""
    accumulator = StreamTelemetryAccumulator()
    accumulator(
        _event(
            StreamOperation.DRAIN,
            StreamOperationOutcome.EOF,
            bytes_consumed=4,
            read_operations=2,
            duration_s=0.25,
        )
    )

    accumulator.add_snapshot(
        StreamTelemetrySnapshot(
            groups={
                (StreamOperation.DRAIN, StreamOperationOutcome.EOF): (
                    StreamTelemetryGroup(
                        bytes_consumed=6,
                        read_operations=3,
                        operation_count=2,
                        duration_seconds=0.5,
                    )
                ),
                (StreamOperation.PIPELINE_TRANSFER, StreamOperationOutcome.EOF): (
                    StreamTelemetryGroup(
                        bytes_consumed=8,
                        read_operations=4,
                        operation_count=1,
                        duration_seconds=1.0,
                    )
                ),
            },
            totals=StreamTelemetryGroup(operation_count=999),
        )
    )

    assert accumulator.snapshot().groups == {
        (StreamOperation.DRAIN, StreamOperationOutcome.EOF): StreamTelemetryGroup(
            bytes_consumed=10,
            read_operations=5,
            operation_count=3,
            duration_seconds=0.75,
        ),
        (StreamOperation.PIPELINE_TRANSFER, StreamOperationOutcome.EOF): (
            StreamTelemetryGroup(
                bytes_consumed=8,
                read_operations=4,
                operation_count=1,
                duration_seconds=1.0,
            )
        ),
    }, "merged groups must add to the matching group and leave others intact"


def test_accumulator_reset_discards_prior_measurements() -> None:
    """Reset returns the accumulator to an empty snapshot."""
    accumulator = StreamTelemetryAccumulator()
    accumulator(
        _event(
            StreamOperation.DRAIN,
            StreamOperationOutcome.EOF,
            bytes_consumed=4,
            read_operations=2,
            duration_s=0.25,
        )
    )

    accumulator.reset()

    assert accumulator.snapshot() == StreamTelemetrySnapshot.empty(), (
        "reset must discard every previously accumulated group"
    )


@pytest.mark.parametrize(
    ("operation", "outcome"),
    [
        pytest.param(
            typ.cast("StreamOperation", "unknown_operation"),
            StreamOperationOutcome.EOF,
            id="unknown-operation",
        ),
        pytest.param(
            StreamOperation.DRAIN,
            typ.cast("StreamOperationOutcome", "unknown_outcome"),
            id="unknown-outcome",
        ),
    ],
)
def test_accumulator_discards_unknown_operation_or_outcome(
    operation: StreamOperation,
    outcome: StreamOperationOutcome,
) -> None:
    """Accumulator rejects each invalid label without relying on the other."""
    accumulator = StreamTelemetryAccumulator()
    accumulator(
        StreamOperationEvent(
            operation=operation,
            outcome=outcome,
            bytes_consumed=1,
            read_operations=1,
            duration_s=1.0,
        )
    )

    assert accumulator.snapshot() == StreamTelemetrySnapshot.empty()
