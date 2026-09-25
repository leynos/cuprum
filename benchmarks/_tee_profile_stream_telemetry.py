"""Aggregate bounded stream-operation telemetry for tee profiling reports."""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import math
import types
import typing as typ

from cuprum.stream_events import (
    StreamOperation,
    StreamOperationEvent,
    StreamOperationOutcome,
)


class StreamTelemetryGroupPayload(typ.TypedDict):
    """JSON-compatible measurements for one stream-operation group."""

    bytes_consumed: int
    read_operations: int
    operation_count: int
    duration_seconds: float


class StreamTelemetryPayload(typ.TypedDict):
    """JSON-compatible aggregate telemetry from one profiling worker."""

    groups: dict[str, dict[str, StreamTelemetryGroupPayload]]
    totals: StreamTelemetryGroupPayload


@dc.dataclass(frozen=True, slots=True)
class StreamTelemetryGroup:
    """Aggregate measurements for one closed stream-operation group.

    Parameters
    ----------
    bytes_consumed:
        Total bytes consumed by completed operations in the group.
    read_operations:
        Total completed reader calls, including EOF reads.
    operation_count:
        Number of completed operations in the group.
    duration_seconds:
        Sum of monotonic operation durations in seconds.
    """

    bytes_consumed: int = 0
    read_operations: int = 0
    operation_count: int = 0
    duration_seconds: float = 0.0

    def as_dict(self) -> StreamTelemetryGroupPayload:
        """Return the group in its stable JSON representation."""
        return {
            "bytes_consumed": self.bytes_consumed,
            "read_operations": self.read_operations,
            "operation_count": self.operation_count,
            "duration_seconds": self.duration_seconds,
        }

    def merged_with(self, other: StreamTelemetryGroup) -> StreamTelemetryGroup:
        """Return a group that combines this group with ``other``."""
        return StreamTelemetryGroup(
            bytes_consumed=self.bytes_consumed + other.bytes_consumed,
            read_operations=self.read_operations + other.read_operations,
            operation_count=self.operation_count + other.operation_count,
            duration_seconds=self.duration_seconds + other.duration_seconds,
        )


@dc.dataclass(frozen=True, slots=True)
class StreamTelemetrySnapshot:
    """Immutable aggregate telemetry grouped by closed operation and outcome.

    Parameters
    ----------
    groups:
        Measurements keyed by a ``(StreamOperation, StreamOperationOutcome)``
        pair.
    totals:
        Measurements summed over every observed group.
    """

    groups: cabc.Mapping[
        tuple[StreamOperation, StreamOperationOutcome], StreamTelemetryGroup
    ]
    totals: StreamTelemetryGroup

    def __post_init__(self) -> None:
        """Defensively freeze the group mapping captured by this snapshot."""
        object.__setattr__(self, "groups", types.MappingProxyType(dict(self.groups)))

    def as_dict(self) -> StreamTelemetryPayload:
        """Return groups keyed by the stable closed enum string values."""
        serialized_groups: dict[str, dict[str, StreamTelemetryGroupPayload]] = {}
        for operation in StreamOperation:
            outcomes = {
                outcome.value: group.as_dict()
                for outcome in StreamOperationOutcome
                if (group := self.groups.get((operation, outcome))) is not None
            }
            if outcomes:
                serialized_groups[operation.value] = outcomes
        return {"groups": serialized_groups, "totals": self.totals.as_dict()}

    @classmethod
    def from_dict(cls, payload: object) -> StreamTelemetrySnapshot:
        """Build a snapshot from a profiling-worker JSON payload.

        Unknown group names and malformed entries are ignored so a failed or
        older worker sample cannot manufacture labels in the sweep summary.

        Returns
        -------
        StreamTelemetrySnapshot
            The recognized closed operation and outcome groups, or an empty
            snapshot when the payload has no valid group mapping.
        """
        if not isinstance(payload, cabc.Mapping):
            return cls.empty()
        raw_groups = payload.get("groups")
        if not isinstance(raw_groups, cabc.Mapping):
            return cls.empty()

        groups: dict[
            tuple[StreamOperation, StreamOperationOutcome], StreamTelemetryGroup
        ] = {}
        for operation in StreamOperation:
            raw_outcomes = raw_groups.get(operation.value)
            if not isinstance(raw_outcomes, cabc.Mapping):
                continue
            for outcome in StreamOperationOutcome:
                group = _group_from_dict(raw_outcomes.get(outcome.value))
                if group is not None:
                    groups[operation, outcome] = group
        return cls(groups=groups, totals=_totals(groups.values()))

    @classmethod
    def empty(cls) -> StreamTelemetrySnapshot:
        """Return an empty snapshot with zero overall totals."""
        return cls(groups={}, totals=StreamTelemetryGroup())


class StreamTelemetryAccumulator:
    """Accumulate bounded stream-operation completion events for one run."""

    __slots__ = ("_groups",)

    def __init__(self) -> None:
        """Initialize an empty accumulator."""
        self._groups: dict[
            tuple[StreamOperation, StreamOperationOutcome], StreamTelemetryGroup
        ] = {}

    def __call__(self, event: StreamOperationEvent) -> None:
        """Add one event when its operation and outcome use the closed enums."""
        if not isinstance(event.operation, StreamOperation) or not isinstance(
            event.outcome,
            StreamOperationOutcome,
        ):
            return
        key = (event.operation, event.outcome)
        observed = StreamTelemetryGroup(
            bytes_consumed=event.bytes_consumed,
            read_operations=event.read_operations,
            operation_count=1,
            duration_seconds=event.duration_s,
        )
        self._groups[key] = self._groups.get(key, StreamTelemetryGroup()).merged_with(
            observed
        )

    def add_snapshot(self, snapshot: StreamTelemetrySnapshot) -> None:
        """Merge groups from one worker snapshot into this accumulator."""
        for key, group in snapshot.groups.items():
            self._groups[key] = self._groups.get(
                key,
                StreamTelemetryGroup(),
            ).merged_with(group)

    def reset(self) -> None:
        """Discard measurements from any prior profiling run."""
        self._groups = {}

    def snapshot(self) -> StreamTelemetrySnapshot:
        """Return an immutable view of all measurements accumulated so far."""
        groups = dict(self._groups)
        return StreamTelemetrySnapshot(groups=groups, totals=_totals(groups.values()))


def _group_from_dict(value: object) -> StreamTelemetryGroup | None:
    """Return a group only when every serialized aggregate field is valid."""
    if not isinstance(value, cabc.Mapping):
        return None
    bytes_consumed = value.get("bytes_consumed")
    read_operations = value.get("read_operations")
    operation_count = value.get("operation_count")
    duration_seconds = value.get("duration_seconds")
    duration = _duration_from_value(duration_seconds)
    if duration is None:
        return None
    integer_values = (
        _integer_from_value(bytes_consumed),
        _integer_from_value(read_operations),
        _integer_from_value(operation_count),
    )
    match integer_values:
        case (
            int() as recognized_bytes_consumed,
            int() as recognized_read_operations,
            int() as recognized_operation_count,
        ):
            return StreamTelemetryGroup(
                bytes_consumed=recognized_bytes_consumed,
                read_operations=recognized_read_operations,
                operation_count=recognized_operation_count,
                duration_seconds=duration,
            )
        case _:
            return None


def _integer_from_value(value: object) -> int | None:
    """Return a serialized aggregate integer while excluding booleans."""
    match value:
        case bool():
            return None
        case int() if value >= 0:
            return value
        case _:
            return None


def _duration_from_value(value: object) -> float | None:
    """Return a serialized duration as a float while excluding booleans."""
    match value:
        case bool():
            return None
        case (int() | float()) as numeric_duration:
            duration = float(numeric_duration)
            return duration if math.isfinite(duration) and duration >= 0.0 else None
        case _:
            return None


def _totals(groups: cabc.Iterable[StreamTelemetryGroup]) -> StreamTelemetryGroup:
    """Sum every group into the overall aggregate totals."""
    totals = StreamTelemetryGroup()
    for group in groups:
        totals = totals.merged_with(group)
    return totals


__all__ = [
    "StreamTelemetryAccumulator",
    "StreamTelemetryGroup",
    "StreamTelemetryGroupPayload",
    "StreamTelemetryPayload",
    "StreamTelemetrySnapshot",
]
