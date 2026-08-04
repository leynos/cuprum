"""Metrics adapter for Rust-pump routing events.

Counts the two pump lifecycle facts that are otherwise visible only as ``DEBUG``
log records: how often an inter-stage hop declined the Rust pump, broken down by
the seam that refused, and how often a cancelled hop's worker had failed.

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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.adapters.metrics_adapter import MetricsCollector
    from cuprum.pump_events import PumpEvent, PumpHook, PumpPhase

RUST_PUMP_DECLINED_TOTAL = "cuprum_rust_pump_declined_total"
"""Declined Rust-pump hand-offs, labelled by the ``reason`` that refused."""

RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL = "cuprum_rust_pump_failed_after_cancel_total"
"""Rust-pump worker failures consumed after their hop was cancelled."""

# One counter per phase, keyed by phase so the metric names have exactly one
# definition. Read-only so an importing module cannot rewrite them at runtime.
_PHASE_COUNTERS: cabc.Mapping[str, str] = types.MappingProxyType({
    "declined": RUST_PUMP_DECLINED_TOTAL,
    "failed_after_cancel": RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL,
})

_UNKNOWN_REASON = "unknown"


def _phase_labels(event: PumpEvent) -> dict[str, str]:
    """Return the bounded label set for ``event``.

    Only a decline is labelled, and only by its closed-set reason. Nothing
    derived from a descriptor, an argument vector, or an exception reaches a
    label, so the series count stays fixed at the size of that enum.
    """
    if event.phase != "declined":
        return {}
    return {
        "reason": str(event.reason) if event.reason is not None else _UNKNOWN_REASON
    }


class PumpMetricsHook:
    """Pump observation hook that counts Rust-pump routing decisions.

    The hook emits:

    - ``cuprum_rust_pump_declined_total``: incremented once per hop that fell
      back to the Python pump, labelled ``reason`` with one of
      ``raw_fd_unavailable``, ``reader_pause_failed``, or
      ``blocking_mode_unavailable``.
    - ``cuprum_rust_pump_failed_after_cancel_total``: incremented once,
      unlabelled, per Rust-pump worker failure recovered after its hop was
      cancelled.

    A successful hand-off increments nothing; there is no pump event for it.

    An unrecognized phase is ignored rather than raised on. That is the lesson
    of :class:`~cuprum.adapters.metrics_adapter._UnhandledMetricsPhaseError`,
    whose fail-closed match is why pump events are not an ``ExecPhase``: a hook
    that raises on a phase it has not heard of turns a library-side addition
    into a failure inside code that was correct when it was written. The phase
    set is a closed :data:`~cuprum.pump_events.PumpPhase` literal, so a
    misspelling is a type error rather than a silent drop.

    Parameters
    ----------
    collector:
        A :class:`~cuprum.adapters.metrics_adapter.MetricsCollector`
        implementation for the target backend. It must be thread-safe: a
        decline can be recorded from any task running a pipe hop.

    Example
    -------
    ::

        metrics = InMemoryMetrics()

        with observe_pump(PumpMetricsHook(metrics)):
            pipeline.run_sync()

        assert metrics.counters["cuprum_rust_pump_declined_total"] == 1.0

    """

    __slots__ = ("_collector",)

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
        counter_name = _PHASE_COUNTERS.get(phase)
        if counter_name is None:
            return
        self._collector.inc_counter(counter_name, 1.0, _phase_labels(event))


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
    "RUST_PUMP_DECLINED_TOTAL",
    "RUST_PUMP_FAILED_AFTER_CANCEL_TOTAL",
    "PumpMetricsHook",
    "pump_metrics_hook",
]
