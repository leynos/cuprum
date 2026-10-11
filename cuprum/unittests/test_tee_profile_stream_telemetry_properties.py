"""Property tests for bounded aggregate tee-profile stream telemetry.

``test_tee_profile_stream_telemetry.py`` pins the example cases: which
serialized shapes are accepted, which labels are closed, and what one known
group sums to. These properties cover the invariants that hold for *any*
sequence of events or samples, where a table of examples cannot reach:
cumulative integer totals are order-insensitive and independent of how events
are split, a snapshot round-trips through its JSON form, and malformed payloads
are always discarded rather than raised.

Durations are summed as binary64 floats, which are not associative, so the
duration assertions compare within a relative tolerance while the integer
counters are asserted exactly. Comparing durations exactly would fail on
legitimate input the moment the grouping order changed.
"""

from __future__ import annotations

import math
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_OPERATIONS = st.sampled_from(list(StreamOperation))
_OUTCOMES = st.sampled_from(list(StreamOperationOutcome))

# Counter values stay small so that summing a generated sequence stays well
# inside the range where integer arithmetic is exact.
_COUNTERS = st.integers(min_value=0, max_value=1 << 20)
_DURATIONS = st.floats(
    min_value=0.0,
    max_value=1000.0,
    allow_nan=False,
    allow_infinity=False,
)

_GROUP_KEYS = st.tuples(_OPERATIONS, _OUTCOMES)

# A relative tolerance wide enough for the reordering error of a handful of
# double-precision additions, and far tighter than any difference the
# aggregation contract treats as meaningful.
_DURATION_REL_TOLERANCE = 1e-9


@st.composite
def _events(draw: st.DrawFn, *, max_size: int = 12) -> list[StreamOperationEvent]:
    """Generate a sequence of completed-operation events."""
    return draw(
        st.lists(
            st.builds(
                StreamOperationEvent,
                operation=_OPERATIONS,
                outcome=_OUTCOMES,
                bytes_consumed=_COUNTERS,
                read_operations=_COUNTERS,
                duration_s=_DURATIONS,
            ),
            max_size=max_size,
        )
    )


@st.composite
def _snapshots(
    draw: st.DrawFn,
    *,
    max_groups: int = 6,
) -> StreamTelemetrySnapshot:
    """Generate a snapshot with distinct keys and nonnegative group values."""
    keys = draw(st.lists(_GROUP_KEYS, max_size=max_groups, unique=True))
    groups = {
        key: StreamTelemetryGroup(
            bytes_consumed=draw(_COUNTERS),
            read_operations=draw(_COUNTERS),
            operation_count=draw(_COUNTERS),
            duration_seconds=draw(_DURATIONS),
        )
        for key in keys
    }
    return StreamTelemetrySnapshot(
        groups=groups,
        totals=_snapshot_totals(groups.values()),
    )


def _snapshot_totals(
    groups: cabc.Iterable[StreamTelemetryGroup],
) -> StreamTelemetryGroup:
    """Sum groups into an expected total, mirroring the production contract."""
    totals = StreamTelemetryGroup()
    for group in groups:
        totals = totals.merged_with(group)
    return totals


def _accumulate(
    events: cabc.Iterable[StreamOperationEvent],
) -> StreamTelemetryAccumulator:
    """Feed events to a fresh accumulator and return it."""
    accumulator = StreamTelemetryAccumulator()
    for event in events:
        accumulator(event)
    return accumulator


def _assert_groups_close(
    actual: StreamTelemetryGroup,
    expected: StreamTelemetryGroup,
    *,
    context: str,
) -> None:
    """Assert two groups agree on counters exactly and on duration closely."""
    assert actual.bytes_consumed == expected.bytes_consumed, (
        f"{context}: bytes_consumed must match"
    )
    assert actual.read_operations == expected.read_operations, (
        f"{context}: read_operations must match"
    )
    assert actual.operation_count == expected.operation_count, (
        f"{context}: operation_count must match"
    )
    assert math.isclose(
        actual.duration_seconds,
        expected.duration_seconds,
        rel_tol=_DURATION_REL_TOLERANCE,
        abs_tol=0.0,
    ), f"{context}: duration_seconds must agree, got {actual} vs {expected}"


def _assert_snapshots_equivalent(
    actual: StreamTelemetrySnapshot,
    expected: StreamTelemetrySnapshot,
    *,
    context: str,
) -> None:
    """Assert two snapshots describe the same measurements."""
    assert set(actual.groups) == set(expected.groups), (
        f"{context}: the same operation and outcome groups must be present"
    )
    for key, expected_group in expected.groups.items():
        _assert_groups_close(
            actual.groups[key],
            expected_group,
            context=f"{context}: {key[0].value}/{key[1].value}",
        )
    _assert_groups_close(actual.totals, expected.totals, context=f"{context}: totals")


@given(events=_events())
def test_accumulation_is_invariant_under_event_permutation(
    events: list[StreamOperationEvent],
) -> None:
    """Reordering events never changes the resulting groups or totals."""
    expected = _accumulate(events).snapshot()

    reversed_snapshot = _accumulate(reversed(events)).snapshot()

    _assert_snapshots_equivalent(
        reversed_snapshot,
        expected,
        context="reversed event order",
    )


@given(events=_events())
def test_grouping_events_by_key_matches_adding_each_event(
    events: list[StreamOperationEvent],
) -> None:
    """Splitting a run in two and merging the halves is equivalent."""
    split = len(events) // 2
    lower, upper = _accumulate(events[:split]), _accumulate(events[split:])

    merged = StreamTelemetryAccumulator()
    merged.add_snapshot(lower.snapshot())
    merged.add_snapshot(upper.snapshot())

    _assert_snapshots_equivalent(
        merged.snapshot(),
        _accumulate(events).snapshot(),
        context="merged halves",
    )


@given(snapshot=_snapshots())
def test_snapshot_round_trips_through_its_json_payload(
    snapshot: StreamTelemetrySnapshot,
) -> None:
    """Serializing a snapshot and parsing it back preserves every measurement."""
    restored = StreamTelemetrySnapshot.from_dict(snapshot.as_dict())

    assert restored.groups == snapshot.groups, (
        "every accepted group must survive the round trip unchanged"
    )
    _assert_groups_close(
        restored.totals,
        snapshot.totals,
        context="round-tripped totals",
    )


@given(snapshot=_snapshots())
def test_serialized_snapshot_reparses_identically(
    snapshot: StreamTelemetrySnapshot,
) -> None:
    """Reparsing a serialized snapshot is idempotent regardless of group order."""
    once = StreamTelemetrySnapshot.from_dict(snapshot.as_dict())

    twice = StreamTelemetrySnapshot.from_dict(once.as_dict())

    assert twice == once, "reparsing a snapshot must reproduce it exactly"


@given(snapshot=_snapshots())
def test_reset_returns_an_empty_snapshot(
    snapshot: StreamTelemetrySnapshot,
) -> None:
    """Reset discards every measurement, whatever was accumulated first."""
    accumulator = StreamTelemetryAccumulator()
    accumulator.add_snapshot(snapshot)

    accumulator.reset()

    assert accumulator.snapshot() == StreamTelemetrySnapshot.empty(), (
        "reset must clear groups and totals regardless of prior measurements"
    )


@given(
    payload=st.one_of(
        st.none(),
        st.integers(),
        st.text(),
        st.lists(st.integers()),
        st.dictionaries(st.text(), st.integers()),
        st.dictionaries(
            st.sampled_from([operation.value for operation in StreamOperation]),
            st.one_of(st.none(), st.integers(), st.text(), st.lists(st.integers())),
        ),
    )
)
def test_arbitrary_payloads_never_raise(payload: object) -> None:
    """Untrusted payloads are discarded rather than raising into the sweep."""
    snapshot = StreamTelemetrySnapshot.from_dict(payload)

    assert isinstance(snapshot, StreamTelemetrySnapshot), (
        "parsing must always yield a snapshot"
    )
    _assert_snapshots_equivalent(
        snapshot,
        StreamTelemetrySnapshot(
            groups=snapshot.groups,
            totals=_snapshot_totals(snapshot.groups.values()),
        ),
        context="arbitrary payload",
    )


@given(
    duration=st.integers(min_value=10**309, max_value=10**312)
    | st.sampled_from([float("inf"), float("-inf")])
)
def test_durations_outside_the_binary64_range_are_discarded(
    duration: int | float,
) -> None:
    """A duration no finite nonnegative float can hold discards its group."""
    payload = {
        "groups": {
            StreamOperation.DRAIN.value: {
                StreamOperationOutcome.EOF.value: {
                    "bytes_consumed": 1,
                    "read_operations": 1,
                    "operation_count": 1,
                    "duration_seconds": duration,
                }
            }
        }
    }

    snapshot = StreamTelemetrySnapshot.from_dict(payload)

    assert snapshot == StreamTelemetrySnapshot.empty(), (
        f"an unrepresentable duration must discard the group, got {duration!r}"
    )


@pytest.mark.parametrize("operation", list(StreamOperation))
@pytest.mark.parametrize("outcome", list(StreamOperationOutcome))
def test_every_closed_label_pair_can_participate(
    operation: StreamOperation,
    outcome: StreamOperationOutcome,
) -> None:
    """The full closed vocabulary survives a round trip through JSON."""
    accumulator = StreamTelemetryAccumulator()
    accumulator(
        StreamOperationEvent(
            operation=operation,
            outcome=outcome,
            bytes_consumed=3,
            read_operations=2,
            duration_s=0.5,
        )
    )

    serialized = accumulator.snapshot().as_dict()

    assert StreamTelemetrySnapshot.from_dict(serialized).groups == {
        (operation, outcome): StreamTelemetryGroup(
            bytes_consumed=3,
            read_operations=2,
            operation_count=1,
            duration_seconds=0.5,
        )
    }, f"the closed pair {(operation.value, outcome.value)} must round trip"
