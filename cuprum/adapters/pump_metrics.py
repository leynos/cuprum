"""Metrics adapter for Rust-pump routing events.

Counts the pump lifecycle facts that are otherwise visible only as ``DEBUG``
log records: how often an inter-stage hop declined the Rust pump, broken down by
the seam that refused; how often a cancelled hop's worker failed; and how long
native cleanup took after cancellation.

The hook consumes :class:`~cuprum.pump_events.PumpEvent` values from
:func:`~cuprum.pump_observation.observe_pump`, not
:class:`~cuprum.events.ExecEvent` values from ``sh.observe``. It reuses the
:class:`~cuprum.adapters.metrics_adapter.MetricsCollector` protocol, so one
collector can back both hooks and no new telemetry dependency is introduced.

Example
-------
::

    from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
    from cuprum.adapters.pump_metrics import PumpMetricsHook
    from cuprum.pump_observation import observe_pump

    metrics = InMemoryMetrics()

    with sh.observe(MetricsHook(metrics)), observe_pump(PumpMetricsHook(metrics)):
        pipeline.run_sync()

    print(metrics.counters["cuprum_rust_pump_declined_total"])

"""

from __future__ import annotations

import types
import typing as typ

from cuprum.pump_events import RustPumpDeclineReason, RustPumpHandoffOutcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.metrics_adapter import MetricsCollector
    from cuprum.pump_events import PumpEvent, PumpHook, PumpPhase

RUST_PUMP_DECLINED_TOTAL = "cuprum_rust_pump_declined_total"
"""Declined Rust-pump hand-offs, labelled by the ``reason`` that refused."""

RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL = "cuprum_rust_pump_failed_after_cancel_total"
"""Rust-pump worker failures consumed after their hop was cancelled."""

RUST_PUMP_CLEANUP_TOTAL = "cuprum_rust_pump_cleanup_total"
"""Completed native-pump cleanups after a pipeline cancellation."""

RUST_PUMP_CLEANUP_DURATION_SECONDS = "cuprum_rust_pump_cleanup_duration_seconds"
"""Monotonic wait durations for completed native-pump cleanup."""

RUST_PUMP_HANDOFF_TOTAL = "cuprum_rust_pump_handoff_total"
"""Rust writer-resource hand-offs, labelled by their bounded outcome."""

RUST_PUMP_CLEANUP_GRACE_EXPIRED_TOTAL = "cuprum_rust_pump_cleanup_grace_expired_total"
"""Caller-facing cleanup waits that exhausted their configured grace."""

RUST_PUMP_CLEANUP_DEFERRED_TOTAL = "cuprum_rust_pump_cleanup_deferred_total"
"""Deferred native-pump cleanups completed after the caller had returned."""

# One counter per phase, keyed by phase so the metric names have exactly one
# definition. Read-only so an importing module cannot rewrite them at runtime.
_PHASE_COUNTERS: cabc.Mapping[str, str] = types.MappingProxyType({
    "declined": RUST_PUMP_DECLINED_TOTAL,
    "failed_after_cancel": RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL,
    "cleanup_completed": RUST_PUMP_CLEANUP_TOTAL,
    "handoff": RUST_PUMP_HANDOFF_TOTAL,
    "cleanup_grace_expired": RUST_PUMP_CLEANUP_GRACE_EXPIRED_TOTAL,
    "cleanup_deferred": RUST_PUMP_CLEANUP_DEFERRED_TOTAL,
})

UNKNOWN_DECLINE_REASON = "unknown"
"""The ``reason`` label a decline carrying no recognized reason degrades to.

Public because it is operator-visible: it is the one value the ``reason`` label
can take that is not a :class:`~cuprum.pump_events.RustPumpDeclineReason`
member, so a dashboard filtering on that enum alone would silently miss these.
Declared here rather than as an enum member because no seam reports it — it
exists only so a malformed event cannot introduce an unbounded label.
"""


def _phase_labels(event: PumpEvent) -> dict[str, str]:
    """Return the bounded label set for ``event``.

    Only declines and hand-offs are labelled, and only by their closed sets.
    Nothing derived from a descriptor, an argument vector, or an exception
    reaches a label, so the series count remains fixed.

    The reason is checked against the enum at run time rather than trusted from
    the annotation. :class:`~cuprum.pump_events.PumpEvent` is a public,
    caller-constructible dataclass with no runtime validation, so a hook fed an
    event carrying an arbitrary object would otherwise put ``str(object)`` on a
    metric label — one series per value, which is the unbounded cardinality this
    label set exists to rule out. Type checking cannot close that hole for a
    caller who is not type checked.

    Returns
    -------
    dict[str, str]
        The fixed labels allowed for the event's metric series.
    """
    if event.phase == "declined":
        reason = event.reason
        if not isinstance(reason, RustPumpDeclineReason):
            return {"reason": UNKNOWN_DECLINE_REASON}
        return {"reason": str(reason)}
    if event.phase == "handoff" and isinstance(
        event.outcome,
        RustPumpHandoffOutcome,
    ):
        return {"outcome": str(event.outcome)}
    return {}


class PumpMetricsHook:
    """Collect bounded metrics from Rust-pump routing events."""

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the pump metrics hook with a collector."""
        self._collector = collector

    def __call__(self, event: PumpEvent) -> None:
        """Increment the counter this pump event calls for, if any.

        A collector that raises does not reach the pump: the emitter records
        the failure and lets the hop continue, so a broken metrics backend
        cannot fail a pipeline that would otherwise have succeeded. See
        :func:`cuprum.pump_observation._emit_pump_event`.
        """
        phase: PumpPhase = event.phase
        if phase == "handoff" and not isinstance(
            event.outcome,
            RustPumpHandoffOutcome,
        ):
            return
        counter_name = _PHASE_COUNTERS.get(phase)
        if counter_name is not None:
            self._collector.inc_counter(counter_name, 1.0, _phase_labels(event))
        if phase != "cleanup_completed" or event.duration_s is None:
            return
        self._collector.observe_histogram(
            RUST_PUMP_CLEANUP_DURATION_SECONDS,
            event.duration_s,
            {},
        )


def pump_metrics_hook(collector: MetricsCollector) -> PumpHook:
    """Create a pump observation hook for the given collector.

    Parameters
    ----------
    collector:
        A :class:`~cuprum.adapters.metrics_adapter.MetricsCollector`
        implementation.

    Returns
    -------
    PumpHook
        A hook suitable for use with
        :func:`~cuprum.pump_observation.observe_pump`.

    Examples
    --------
    ::

        with observe_pump(pump_metrics_hook(metrics)):
            pipeline.run_sync()

    """
    return PumpMetricsHook(collector)


__all__ = [
    "RUST_PUMP_CLEANUP_DEFERRED_TOTAL",
    "RUST_PUMP_CLEANUP_DURATION_SECONDS",
    "RUST_PUMP_CLEANUP_GRACE_EXPIRED_TOTAL",
    "RUST_PUMP_CLEANUP_TOTAL",
    "RUST_PUMP_DECLINED_TOTAL",
    "RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL",
    "RUST_PUMP_HANDOFF_TOTAL",
    "UNKNOWN_DECLINE_REASON",
    "PumpMetricsHook",
    "pump_metrics_hook",
]
