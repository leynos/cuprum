"""Bounded metrics for the control operations an owned handle performs."""

from __future__ import annotations

import typing as typ

from cuprum.control_events import (
    ControlEvent,
    ControlHook,
    ControlOperation,
    ControlOutcome,
)

if typ.TYPE_CHECKING:
    from cuprum.adapters.metrics_adapter import MetricsCollector

CONTROL_OPERATIONS_TOTAL = "cuprum_control_operations_total"
CONTROL_OPERATION_DURATION_SECONDS = "cuprum_control_operation_duration_seconds"
CONTROL_REFUSALS_TOTAL = "cuprum_control_refusals_total"

_REFUSED_OUTCOMES = frozenset(
    {ControlOutcome.REFUSED_EXITED, ControlOutcome.REFUSED_UNSUPPORTED},
)
"""The outcomes that mean the child was never reached at all."""


def _is_refused(outcome: ControlOutcome) -> bool:
    """Report whether one closed outcome chose the refusing arm."""
    return outcome in _REFUSED_OUTCOMES


class ControlMetricsHook:
    """Project control events into bounded metrics.

    The hook emits one counter and one duration histogram for every received
    event. Both metrics carry exactly the closed ``operation`` and ``outcome``
    labels from the event enums, so the series count is fixed by the two enums
    rather than by how many children a process happens to control. No PID,
    execution identifier, or command argument reaches a label.
    """

    __slots__ = ("_collector",)

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the hook with a thread-safe metrics collector."""
        self._collector = collector

    def __call__(self, event: ControlEvent) -> None:
        """Record the control metrics for a recognized closed event."""
        labels = _control_labels(event)
        if labels is None:
            return
        self._collector.inc_counter(CONTROL_OPERATIONS_TOTAL, 1.0, labels)
        self._collector.observe_histogram(
            CONTROL_OPERATION_DURATION_SECONDS,
            event.duration_s,
            labels,
        )
        if _is_refused(event.outcome):
            self._collector.inc_counter(
                CONTROL_REFUSALS_TOTAL,
                1.0,
                {"operation": labels["operation"]},
            )


def _control_labels(event: ControlEvent) -> dict[str, str] | None:
    """Return labels only when both caller-constructible fields are closed.

    An event is dataclass-constructible with any string in either field, so a
    caller can build one whose values are outside the enums. Re-validating here
    rather than trusting the annotation is what keeps the label set closed even
    for an event no handle emitted.

    Returns
    -------
    dict[str, str] | None
        The ``operation`` and ``outcome`` labels, or ``None`` when either field
        is not a member of its enum.

    """
    if not isinstance(event.operation, ControlOperation) or not isinstance(
        event.outcome,
        ControlOutcome,
    ):
        return None
    return {"operation": str(event.operation), "outcome": str(event.outcome)}


def control_metrics_hook(collector: MetricsCollector) -> ControlHook:
    """Create a bounded metrics hook for control observation."""
    return ControlMetricsHook(collector)


__all__ = [
    "CONTROL_OPERATIONS_TOTAL",
    "CONTROL_OPERATION_DURATION_SECONDS",
    "CONTROL_REFUSALS_TOTAL",
    "ControlMetricsHook",
    "control_metrics_hook",
]
