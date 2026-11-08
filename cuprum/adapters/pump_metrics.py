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

import threading
import types
import typing as typ
import weakref

from cuprum.adapters.gauge_metrics import GaugeMetricsCollector
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

RUST_PUMP_HELD_PAIRS = "cuprum_rust_pump_held_pairs"
"""Descriptor pairs currently held across a pending native-pump drain.

A gauge, reported only to collectors that implement
:class:`~cuprum.adapters.gauge_metrics.GaugeMetricsCollector`. It rises by one
when a hop takes its acquired pair and falls by one on every settling path, so
it returns to zero when no drain is pending.
"""

# The only deltas a hold event may carry. A hand-built event with any other
# value is ignored rather than allowed to corrupt the gauge.
_HELD_DELTAS = frozenset({-1, 1})

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


class _HeldPairCounter:
    """The held-pair count for one collector, shared by its reporting hooks."""

    def __init__(self) -> None:
        """Start with no held pairs under a lock that serializes publication."""
        self.lock = threading.Lock()
        self.pairs = 0


# The count belongs to the collector, not to a hook: the metric name is
# unlabelled, so every hook publishing to one collector must publish the same
# running total. Keys are object identities because a collector need not be
# hashable, and the weak reference drops an entry once its collector is gone.
_COUNTERS: dict[
    int,
    tuple[weakref.ReferenceType[object], _HeldPairCounter],
] = {}
# Reentrant because a weakref callback can drop the last reference to another
# collector while this lock is already held on the collecting thread.
_COUNTERS_LOCK = threading.RLock()


def _forget_counter(key: int, reference: weakref.ReferenceType[object]) -> None:
    """Drop a retired collector's counter unless its identity was reused."""
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        if entry is not None and entry[0] is reference:
            del _COUNTERS[key]


def _shared_held_counter(collector: object) -> _HeldPairCounter:
    """Return the held-pair counter every hook for ``collector`` shares.

    A release from one hook must not publish zero while another hook still
    holds a pair, so hooks with the same collector move one counter rather
    than one each. The gauge is unlabelled and collector-wide, which makes the
    collector the natural scope. A collector that cannot be weakly referenced
    still counts correctly for its hook; it just cannot share the count with a
    second hook.

    Returns
    -------
    _HeldPairCounter
        The counter this hook must move when a hold is taken or released.
    """
    key = id(collector)
    with _COUNTERS_LOCK:
        entry = _COUNTERS.get(key)
        if entry is not None and entry[0]() is collector:
            return entry[1]
        counter = _HeldPairCounter()
        try:
            reference = weakref.ref(
                collector,
                lambda ref, key=key: _forget_counter(key, ref),
            )
        except TypeError:
            return counter
        _COUNTERS[key] = (reference, counter)
        return counter


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
    """Collect bounded metrics from Rust-pump routing events.

    The hook emits the following metrics:

    - ``cuprum_rust_pump_declined_total``: a counter labelled ``reason`` by
      the closed :class:`~cuprum.pump_events.RustPumpDeclineReason` vocabulary,
      plus ``unknown`` for an unrecognized hand-built event.
    - ``cuprum_rust_pump_failed_after_cancel_total``: an unlabelled counter for
      worker failures observed after cancellation.
    - ``cuprum_rust_pump_cleanup_total``: an unlabelled counter for completed
      native-pump cleanup.
    - ``cuprum_rust_pump_cleanup_duration_seconds``: an unlabelled histogram
      of normal cleanup durations.
    - ``cuprum_rust_pump_cleanup_grace_expired_total``: an unlabelled counter
      for cleanup waits that reach their configured grace limit.
    - ``cuprum_rust_pump_cleanup_deferred_total``: an unlabelled counter for
      deferred cleanup completed by the worker's callback.
    - ``cuprum_rust_pump_handoff_total``: a counter labelled ``outcome`` by
      the closed :class:`~cuprum.pump_events.RustPumpHandoffOutcome` vocabulary,
      including the Windows raw-handle rejection.
    - ``cuprum_rust_pump_held_pairs``: a gauge of the descriptor pairs held
      across pending drains, reported only when the collector implements
      :class:`~cuprum.adapters.gauge_metrics.GaugeMetricsCollector`. Hooks that
      report to the same collector share one running total, so one hook's
      release cannot hide another's hold. Gauges are optional so an existing
      two-method collector keeps working.

    All labels are closed and bounded; descriptor values, exception details,
    and other event data are never labels. Unknown phases are ignored so a
    newer producer cannot make an existing observer fail. The collector must
    be thread-safe because pump events may be emitted from worker threads.

    Parameters
    ----------
    collector:
        Thread-safe metrics backend receiving counters and histograms.

    Examples
    --------
    ::

        metrics = InMemoryMetrics()

        with observe_pump(PumpMetricsHook(metrics)):
            pipeline.run_sync()

    """

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the pump metrics hook with a collector."""
        self._collector = collector
        self._gauge: GaugeMetricsCollector | None = (
            typ.cast("GaugeMetricsCollector", collector)
            if isinstance(collector, GaugeMetricsCollector)
            else None
        )
        self._held = (
            _shared_held_counter(collector) if self._gauge is not None else None
        )

    def __call__(self, event: PumpEvent) -> None:
        """Increment the counter this pump event calls for, if any.

        A collector that raises does not reach the pump: the emitter records
        the failure and lets the hop continue, so a broken metrics backend
        cannot fail a pipeline that would otherwise have succeeded. See
        :func:`cuprum.pump_observation._emit_pump_event`.
        """
        phase: PumpPhase = event.phase
        if phase == "handoff_hold":
            self._apply_held_delta(event.held_delta)
            return
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

    def _apply_held_delta(self, delta: int) -> None:
        """Move the held-pair gauge by one balanced acquisition or release.

        The count is kept per collector under a lock because hold events for
        concurrent drains can arrive from different threads, and because
        another hook reporting to the same collector moves the same count. A
        collector that does not offer gauges is left alone rather than
        required to add one.

        Publication happens under the same lock as the count. Publishing after
        releasing it would let two threads publish out of order: a release that
        computed ``0`` could reach the collector first, and the acquisition it
        followed would then overwrite that with a stale ``1`` that no later
        event corrects.
        """
        if self._gauge is None or self._held is None:
            return
        if delta not in _HELD_DELTAS:
            return
        with self._held.lock:
            self._held.pairs += delta
            self._gauge.set_gauge(
                RUST_PUMP_HELD_PAIRS,
                float(self._held.pairs),
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
    "RUST_PUMP_HELD_PAIRS",
    "UNKNOWN_DECLINE_REASON",
    "PumpMetricsHook",
    "pump_metrics_hook",
]
