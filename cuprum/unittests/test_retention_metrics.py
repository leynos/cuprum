"""Metrics projection contract for observe-hook retention samples.

The retention recorder publishes bounded samples; this module pins how the
metrics adapter projects them — the metric names, the values, and the label
set. The label assertions matter as much as the values: the execution's
correlation token must never become a label, or every run would create a new
series and the metric would stop being aggregatable.
"""

from __future__ import annotations

from cuprum._retention_telemetry import (
    _RETENTION_CAPACITY_WAIT,
    _RETENTION_DEPTH,
    _RETENTION_PRUNE,
    _RETENTION_SATURATION,
    _RetentionSample,
)
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.events import new_exec_id
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector

DEPTH = "cuprum_observe_hook_depth"
SATURATIONS = "cuprum_observe_hook_saturations_total"
PRUNED = "cuprum_observe_hook_pruned_total"
PRUNED_FAILURES = "cuprum_observe_hook_pruned_failures_total"
WAIT_SECONDS = "cuprum_observe_hook_capacity_wait_seconds"


def test_depth_sample_is_a_histogram_labelled_by_phase() -> None:
    """A depth sample must land as an observation, not a counter."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)

    hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_DEPTH,
            phase="stdout",
            depth=7,
        )
    )

    assert collector.histograms == [(DEPTH, 7.0, {"phase": "stdout"})], (
        "the depth must be observed under its own metric with the phase label"
    )
    assert collector.counters == [], "a depth sample must not touch a counter"


def test_saturation_sample_counts_one_refusal() -> None:
    """Each refused admission must increment the saturation counter once."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)

    hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_SATURATION,
            phase="stderr",
            depth=256,
        )
    )

    assert collector.counters == [(SATURATIONS, 1.0, {"phase": "stderr"})], (
        "a refusal must be counted once against the phase that saturated"
    )


def test_prune_sample_counts_dropped_and_failed_tasks() -> None:
    """A prune must report both the total dropped and the dropped failures."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)

    hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_PRUNE,
            phase="stdout",
            depth=4,
            pruned=3,
            pruned_failures=1,
        )
    )

    assert collector.counters == [
        (PRUNED, 3.0, {"phase": "stdout"}),
        (PRUNED_FAILURES, 1.0, {"phase": "stdout"}),
    ], "the prune must count every drop and the failures among them"


def test_prune_sample_omits_the_failure_counter_when_none_were_dropped() -> None:
    """A prune that dropped no failure must not emit a zero increment."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)

    hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_PRUNE,
            phase="stdout",
            depth=4,
            pruned=2,
        )
    )

    assert collector.counter_names() == [PRUNED], (
        "only the total-drop counter applies when no failure was dropped"
    )


def test_capacity_wait_sample_is_a_histogram_labelled_by_outcome() -> None:
    """The wait duration must be observed with its closed outcome label."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)

    hook.report_retention(
        _RetentionSample(
            exec_id=new_exec_id(),
            kind=_RETENTION_CAPACITY_WAIT,
            phase="stdout",
            depth=256,
            wait_seconds=0.25,
            outcome="cancelled",
        )
    )

    assert collector.histograms == [
        (WAIT_SECONDS, 0.25, {"phase": "stdout", "outcome": "cancelled"})
    ], "the wait must be observed with the phase and outcome labels"


def test_retention_labels_exclude_the_execution_token() -> None:
    """No retention metric may be labelled by the per-run correlation token."""
    collector = RecordingCollector()
    hook = MetricsHook(collector)
    exec_id = new_exec_id()

    for sample in (
        _RetentionSample(exec_id=exec_id, kind=_RETENTION_DEPTH, phase="stdout"),
        _RetentionSample(exec_id=exec_id, kind=_RETENTION_SATURATION, phase="stdout"),
        _RetentionSample(exec_id=exec_id, kind=_RETENTION_PRUNE, phase="stdout"),
        _RetentionSample(
            exec_id=exec_id,
            kind=_RETENTION_CAPACITY_WAIT,
            phase="stdout",
            outcome="resumed",
        ),
    ):
        hook.report_retention(sample)

    label_keys = {
        key
        for _name, _value, labels in (*collector.counters, *collector.histograms)
        for key in labels
    }
    assert label_keys == {"phase", "outcome"}, (
        "retention labels must stay within the closed phase and outcome sets"
    )
    assert str(exec_id) not in repr(collector.counters + collector.histograms), (
        "the correlation token must not leak into any metric label"
    )


def test_metrics_hook_has_no_instance_dictionary() -> None:
    """The mixin must not hand every hook an unbounded ``__dict__``."""
    hook = MetricsHook(RecordingCollector())

    assert not hasattr(hook, "__dict__"), (
        "a slotted adapter base must declare empty slots, or the subclass's "
        "own slots no longer bound what an instance can carry"
    )
