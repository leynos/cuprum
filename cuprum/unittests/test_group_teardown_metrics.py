"""Metric adapter projections for process-group teardown outcomes.

An owning teardown that left members behind is reported through a
``group_teardown`` execution event, and this module pins what a metrics
collector receives from it. The projection is the only place the signal becomes
a series, so an outcome that reached no counter would be a teardown an operator
could not count — the failure this file exists to make visible.

The two labels are the contract. ``outcome`` is the closed
:class:`~cuprum.events.GroupTeardownOutcome` category, which keeps the series
bounded; the settle duration is observed only where a wait was actually
measured, because recording a zero for an outcome that never waited would drag
every percentile toward a figure that was never taken.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._group_teardown import GroupTeardownReport
from cuprum.adapters._metrics_operations import (
    _PHASE_COUNTERS,
    _CounterOp,
    _group_teardown_operations,
    _HistogramOp,
)
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.events import ExecEvent, GroupTeardownOutcome
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    _make_exec_event,
)

# The measured wait that rides along on exactly one outcome, and the two that
# observed none. Kept as a module constant so the absence cases cannot drift
# apart from the presence case.
_MEASURED_SETTLE_S = 1.0

# Each outcome with the settle duration a real producer pairs with it. The
# producer is the authority here: ``GroupTeardownReport`` leaves ``settle_s``
# at ``None`` and only ``settlement_timeout`` passes one, so a generator that
# drew the two independently would assert against states no run emits.
_OUTCOMES = (
    (GroupTeardownOutcome.SIGNAL_REFUSED, None),
    (GroupTeardownOutcome.UNSIGNALABLE, None),
    (GroupTeardownOutcome.SETTLEMENT_TIMEOUT, _MEASURED_SETTLE_S),
)


def _teardown_event(
    outcome: GroupTeardownOutcome,
    settle_s: float | None,
) -> ExecEvent:
    """Build a ``group_teardown`` event as the producer emits it."""
    return _make_exec_event(
        phase="group_teardown",
        overrides={"group_outcome": outcome, "settle_s": settle_s},
    )


@pytest.mark.parametrize(("outcome", "settle_s"), _OUTCOMES)
def test_every_outcome_increments_a_counter_labelled_by_its_category(
    outcome: GroupTeardownOutcome,
    settle_s: float | None,
) -> None:
    """Each teardown outcome reaches a collector as one bounded-counter call."""
    recorder = _LabelRecordingCollector()

    MetricsHook(recorder)(_teardown_event(outcome, settle_s))

    expected = [
        (
            "cuprum_group_teardowns_total",
            1.0,
            {
                "program": "cat",
                "project": "unknown",
                "outcome": str(outcome),
            },
        )
    ]
    if settle_s is not None:
        expected.append((
            "cuprum_group_settle_seconds",
            settle_s,
            {"program": "cat", "project": "unknown"},
        ))

    assert recorder.calls == expected, (
        f"the {outcome} projection differs: {recorder.calls!r}"
    )


@pytest.mark.parametrize(
    ("outcome", "settle_s"),
    [(o, s) for o, s in _OUTCOMES if s is None],
)
def test_an_outcome_that_measured_no_wait_observes_no_duration(
    outcome: GroupTeardownOutcome,
    settle_s: float | None,
) -> None:
    """A zero would read as an instant wait, so an absent one stays absent."""
    operations = _group_teardown_operations(_teardown_event(outcome, settle_s))

    assert not any(
        isinstance(operation, _HistogramOp) for operation in operations
    ), f"{outcome} measured no wait, so it must observe no duration: {operations!r}"


def test_the_measured_settle_duration_is_observed_as_a_histogram() -> None:
    """The one outcome that measured a wait publishes the figure it measured."""
    operations = _group_teardown_operations(
        _teardown_event(
            GroupTeardownOutcome.SETTLEMENT_TIMEOUT,
            _MEASURED_SETTLE_S,
        )
    )

    assert operations == (
        _CounterOp(
            "cuprum_group_teardowns_total",
            1.0,
            {"outcome": str(GroupTeardownOutcome.SETTLEMENT_TIMEOUT)},
        ),
        _HistogramOp("cuprum_group_settle_seconds", _MEASURED_SETTLE_S),
    ), "the settle wait must be observed as a duration, not counted as one"


def test_the_counter_name_is_absent_from_the_unit_counter_mapping() -> None:
    """The label-bearing counter keeps one definition rather than two."""
    assert "group_teardown" not in _PHASE_COUNTERS, (
        "the phase resolves through its own helper; listing it as a unit "
        "counter would give the metric name a second definition"
    )


def test_a_settle_duration_rides_only_the_outcome_that_measured_one() -> None:
    """The producer is what binds a duration to its outcome.

    ``GroupTeardownReport`` defaults ``settle_s`` to ``None`` and only its
    ``settlement_timeout`` constructor supplies a figure, so the pairing is a
    property of the producer rather than of the projection. Asserting it here
    is what lets the projection test above assert against real events instead
    of drawing two independent values.
    """
    refused = GroupTeardownReport.signal_refused(
        pid=4321,
        owns_group=False,
        signal_number=15,
    )
    timed_out = GroupTeardownReport.settlement_timeout(
        4321,
        settle_s=_MEASURED_SETTLE_S,
    )

    assert refused.settle_s is None, (
        "a refusal never reached a wait, so it must report no duration"
    )
    assert timed_out.settle_s == _MEASURED_SETTLE_S, (
        "the capped wait must carry the figure it measured"
    )


@given(
    settle_s=st.floats(
        min_value=0.0,
        max_value=30.0,
        allow_nan=False,
        allow_infinity=False,
    )
)
def test_the_observed_duration_is_carried_through_unchanged(
    settle_s: float,
) -> None:
    """The projection re-derives, rounds, and drops nothing it is handed."""
    operations = _group_teardown_operations(
        _teardown_event(GroupTeardownOutcome.SETTLEMENT_TIMEOUT, settle_s)
    )

    durations = [
        operation.value
        for operation in operations
        if isinstance(operation, _HistogramOp)
    ]
    assert durations == [settle_s], (
        f"the measured figure must survive the projection; got {durations!r}"
    )


def test_no_outcome_reaches_a_metric_carrying_unbounded_labels() -> None:
    """The counter is labelled by the closed category, never by a pid."""
    pid = 987_654

    for outcome, settle_s in _OUTCOMES:
        collector = _LabelRecordingCollector()
        event = _make_exec_event(
            phase="group_teardown",
            overrides={
                "group_outcome": outcome,
                "settle_s": settle_s,
                "pid": pid,
            },
        )
        MetricsHook(collector)(event)

        assert all(
            str(pid) not in label_value
            for _, _, labels in collector.calls
            for label_value in labels.values()
        ), f"a pid must never become a metric label, saw {collector.calls!r}"
