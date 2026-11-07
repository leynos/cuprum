"""Span-event projection for observe-hook task-retention decisions.

A retention decision explains a run that looks merely slow: the child paused
because the parent could not start another observer. The recorder publishes
those decisions as bounded samples, and this mixin attaches them to the
execution span the sample's correlation token identifies. The capacity wait is
traced at the async boundary that suspended the line reader, with a fixed
operation name and a closed outcome value so the span records whether the wait
resumed, was cancelled, or was interrupted by an unrelated failure.
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

#: Span event names, one per bounded retention surface.
_RETENTION_EVENT = "cuprum.observe_task_retention"
_CAPACITY_WAIT_EVENT = "cuprum.observe_task_capacity_wait"
_CAPACITY_WAIT_OPERATION = "observe_task_capacity_wait"


class _RetentionTracingMixin:
    """Add retention samples to the execution span they correlate with."""

    def report_retention(self, sample: _RetentionSample) -> None:
        """Record one correlated retention sample on its execution span."""
        if sample.kind == _RETENTION_CAPACITY_WAIT:
            name, attributes = _capacity_wait_event(sample)
        else:
            name, attributes = _retention_event(sample)
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


def _retention_event(
    sample: _RetentionSample,
) -> tuple[str, dict[str, object]]:
    """Build the bounded projection of a depth, saturation, or prune sample."""
    attributes: dict[str, object] = {"kind": sample.kind, "depth": sample.depth}
    if sample.phase is not None:
        attributes["phase"] = sample.phase
    if sample.kind == _RETENTION_PRUNE:
        attributes["pruned"] = sample.pruned
        attributes["pruned_failures"] = sample.pruned_failures
    return _RETENTION_EVENT, attributes
