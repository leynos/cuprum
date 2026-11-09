"""Span-event projection for observe-hook task-retention decisions.

A retention decision explains a run that looks merely slow: the child paused
because the parent could not start another observer. The recorder publishes
those decisions as bounded samples, and this mixin attaches the samples that
explain a backpressure episode to the execution span their correlation token
identifies. Two kinds qualify: the capacity wait, traced at the async boundary
that suspended the line reader with a fixed operation name and a closed outcome
value, and a prune that actually dropped settled tasks.

Depth and saturation samples are deliberately not projected here. Pruning runs
on the per-line path, so tracing every sample would grow the span with the
line count — the failure class the retention bound removes. Those samples stay
in the log records and the metrics projection, which are bounded by
construction.
"""

from __future__ import annotations

import typing as typ

from cuprum._retention_telemetry import (
    _RETENTION_CAPACITY_WAIT,
    _RETENTION_PRUNE,
)
from cuprum.adapters._tracing_span_events import record_span_event

if typ.TYPE_CHECKING:
    from cuprum._retention_telemetry import _RetentionSample
    from cuprum.adapters.tracing_adapter import TracingHook

#: Span event names, one per traced retention surface.
_RETENTION_EVENT = "cuprum.observe_task_retention"
_CAPACITY_WAIT_EVENT = "cuprum.observe_task_capacity_wait"
_CAPACITY_WAIT_OPERATION = "observe_task_capacity_wait"


class _RetentionTracingMixin:
    """Add retention samples to the execution span they correlate with."""

    def report_retention(self, sample: _RetentionSample) -> None:
        """Record one retention sample that explains a backpressure episode."""
        if sample.kind == _RETENTION_CAPACITY_WAIT:
            name, attributes = _capacity_wait_event(sample)
        elif sample.kind == _RETENTION_PRUNE and sample.pruned > 0:
            name, attributes = _prune_event(sample)
        else:
            return
        record_span_event(
            typ.cast("TracingHook", self),
            sample.exec_id,
            name,
            attributes,
        )


def _capacity_wait_event(
    sample: _RetentionSample,
) -> tuple[str, dict[str, object]]:
    """Build the bounded projection of the capacity-wait async boundary."""
    attributes: dict[str, object] = {
        "operation": _CAPACITY_WAIT_OPERATION,
        "outcome": sample.outcome or "unknown",
        "wait_seconds": sample.wait_seconds,
        "depth": sample.depth,
    }
    if sample.phase is not None:
        attributes["phase"] = sample.phase
    return _CAPACITY_WAIT_EVENT, attributes


def _prune_event(
    sample: _RetentionSample,
) -> tuple[str, dict[str, object]]:
    """Build the bounded projection of a prune that dropped settled tasks."""
    attributes: dict[str, object] = {
        "kind": sample.kind,
        "depth": sample.depth,
        "pruned": sample.pruned,
        "pruned_failures": sample.pruned_failures,
    }
    if sample.phase is not None:
        attributes["phase"] = sample.phase
    return _RETENTION_EVENT, attributes
