"""Metrics projection for observe-hook task-retention samples.

The retention recorder answers a question a duration histogram cannot: whether
a run was slow or paused its child because its observers could not keep up.
This mixin projects the recorder's bounded samples into counters and
histograms. Labels are restricted to the sample's closed phase name and the
closed wait outcome; the execution's correlation token, which changes every
run, is deliberately not a label, so the exported series stay bounded.
"""

from __future__ import annotations

import typing as typ

import cuprum._retention_telemetry as _retention

if typ.TYPE_CHECKING:
    from cuprum._retention_telemetry import _RetentionSample
    from cuprum.adapters.metrics_adapter import MetricsHook

#: Metric names, one per bounded retention surface.
_DEPTH = "cuprum_observe_hook_depth"
_SATURATIONS = "cuprum_observe_hook_saturations_total"
_PRUNED = "cuprum_observe_hook_pruned_total"
_PRUNED_FAILURES = "cuprum_observe_hook_pruned_failures_total"
_CAPACITY_WAIT_SECONDS = "cuprum_observe_hook_capacity_wait_seconds"

#: Label value for a sample whose phase the recorder did not know.
_UNKNOWN = "unknown"


class _RetentionMetricsMixin:
    """Project retention samples into bounded-cardinality metrics."""

    # An empty slot declaration keeps the subclass's own ``__slots__``
    # effective: without it, every ``MetricsHook`` instance would regain a
    # ``__dict__`` through this base.
    __slots__ = ()

    def report_retention(self, sample: _RetentionSample) -> None:
        """Apply one retention sample to the collector."""
        hook = typ.cast("MetricsHook", self)
        labels = {"phase": sample.phase or _UNKNOWN}
        # The patterns are dotted so each one compares against the sample-kind
        # constant rather than capturing it as a new name.
        match sample.kind:
            case _retention._RETENTION_DEPTH:
                hook._collector.observe_histogram(_DEPTH, float(sample.depth), labels)
            case _retention._RETENTION_SATURATION:
                hook._collector.inc_counter(_SATURATIONS, 1.0, labels)
            case _retention._RETENTION_PRUNE:
                hook._collector.inc_counter(_PRUNED, float(sample.pruned), labels)
                if sample.pruned_failures:
                    hook._collector.inc_counter(
                        _PRUNED_FAILURES,
                        float(sample.pruned_failures),
                        labels,
                    )
            case _retention._RETENTION_CAPACITY_WAIT:
                hook._collector.observe_histogram(
                    _CAPACITY_WAIT_SECONDS,
                    sample.wait_seconds,
                    {**labels, "outcome": sample.outcome or _UNKNOWN},
                )
