"""Prometheus-style metrics adapter for Cuprum execution events.

This module provides an observe hook that collects metrics about command
execution in a format compatible with Prometheus client libraries. The
adapter demonstrates how to collect:

- **Counters**: Total executions, failures, output lines
- **Byte counters**: Successful stdin bytes written
- **Histograms**: Execution duration distribution

The implementation uses protocol classes to remain decoupled from specific
metrics libraries. Projects can implement the protocols with their preferred
backend (prometheus_client, statsd, OpenTelemetry metrics, etc.).

Example with the in-memory reference implementation::

    from cuprum import ScopeConfig, scoped, sh
    from cuprum.adapters.metrics_adapter import MetricsHook, InMemoryMetrics

    metrics = InMemoryMetrics()

    with scoped(
        ScopeConfig(allowlist=my_allowlist)
    ), sh.observe(MetricsHook(metrics)):
        sh.make(ECHO)("hello").run_sync()

    print(metrics.counters)  # {'cuprum_executions_total': 1, ...}
    print(metrics.histograms)  # {'cuprum_duration_seconds': [...]}

Example with prometheus_client::

    from prometheus_client import Counter, Histogram
    from cuprum.adapters.metrics_adapter import MetricsCollector, MetricsHook

    class PrometheusMetrics:
        def __init__(self):
            self._exec_total = Counter(
                "cuprum_executions_total",
                "Total command executions",
                ["program", "project"],
            )
            self._duration = Histogram(
                "cuprum_duration_seconds",
                "Execution duration",
                ["program", "project"],
            )

        def inc_counter(self, name, value, labels):
            if name == "cuprum_executions_total":
                self._exec_total.labels(**labels).inc(value)

        def observe_histogram(self, name, value, labels):
            if name == "cuprum_duration_seconds":
                self._duration.labels(**labels).observe(value)

    hook = MetricsHook(PrometheusMetrics())

"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

# `_metric_operations` is used directly below; the rest are re-exported only
# for backward compatibility with call sites and tests that imported them
# from here before the metric-operation planning moved to
# `_metrics_operations`.
from cuprum.adapters._metrics_operations import (
    _PHASE_COUNTERS as _PHASE_COUNTERS,
)
from cuprum.adapters._metrics_operations import (
    _CounterOp as _CounterOp,
)
from cuprum.adapters._metrics_operations import (
    _exit_operations as _exit_operations,
)
from cuprum.adapters._metrics_operations import (
    _HistogramOp as _HistogramOp,
)
from cuprum.adapters._metrics_operations import (
    _metric_operations,
)
from cuprum.adapters._metrics_operations import (
    _MetricOp as _MetricOp,
)
from cuprum.adapters._metrics_operations import (
    _resource_operations as _resource_operations,
)
from cuprum.adapters._metrics_operations import (
    _UnhandledMetricsPhaseError as _UnhandledMetricsPhaseError,
)
from cuprum.adapters._support import (
    _LockedStore,
    _project_tag,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


class MetricsCollector(typ.Protocol):
    """Protocol for metrics collection backends.

    Implementations must be thread-safe; hooks may be invoked from multiple
    threads or async tasks concurrently.
    """

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Increment a counter metric.

        Parameters
        ----------
        name:
            Metric name (e.g., ``cuprum_executions_total``).
        value:
            Amount to increment (usually 1.0).
        labels:
            Label key-value pairs for metric dimensions.

        """
        raise NotImplementedError

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram observation.

        Parameters
        ----------
        name:
            Metric name (e.g., ``cuprum_duration_seconds``).
        value:
            Observed value (e.g., duration in seconds).
        labels:
            Label key-value pairs for metric dimensions.

        """
        raise NotImplementedError


@dc.dataclass
class InMemoryMetrics(_LockedStore):
    """Reference in-memory metrics collector for testing and examples.

    Storage and locking follow the shared
    :class:`~cuprum.adapters._support._LockedStore` contract: every mutator
    holds the lock, and ``reset()``
    clears the store under it.

    Attributes
    ----------
    counters:
        Dict mapping metric names to accumulated counter values.
    histograms:
        Dict mapping metric names to lists of observed values.

    """

    counters: dict[str, float] = dc.field(default_factory=dict)
    histograms: dict[str, list[float]] = dc.field(default_factory=dict)

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Increment a counter, ignoring labels for simplicity."""
        with self._lock:
            self.counters[name] = self.counters.get(name, 0.0) + value

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram observation, ignoring labels for simplicity."""
        with self._lock:
            if name not in self.histograms:
                self.histograms[name] = []
            self.histograms[name].append(value)

    @typ.override
    def _clear(self) -> None:
        """Clear all collected metrics; called under the store lock."""
        self.counters.clear()
        self.histograms.clear()


class MetricsHook:
    """Observe hook that collects Prometheus-style metrics.

    The hook emits the following metrics:

    - ``cuprum_executions_total``: Counter incremented on each ``start`` event
    - ``cuprum_failures_total``: Counter incremented on non-zero exit
    - ``cuprum_duration_seconds``: Histogram of execution durations
    - ``cuprum_stdout_lines_total``: Counter of stdout lines emitted
    - ``cuprum_stderr_lines_total``: Counter of stderr lines emitted
    - ``cuprum_stdin_bytes_total``: Counter of successful stdin bytes written
    - ``cuprum_stdin_errors_total``: Counter of stdin writer failures
    - ``cuprum_timeouts_total``: Counter of subprocess and pipeline expiries
    - ``cuprum_teardown_errors_total``: Counter of consumer drain failures
    - ``cuprum_capture_eof_grace_expired_total``: Counter of capture EOF grace
      expiries with readers still pending
    - ``cuprum_pipeline_fail_fast_total``: Counter of pipelines terminated
      early after their first non-final stage failure
    - ``cuprum_resource_usage_measurements_total``: Counter of terminal events
      that reported how their child's resource figures were obtained
    - ``cuprum_child_max_rss_bytes``: Histogram of per-child maximum RSS, from
      the attributable ``wait4`` path only
    - ``cuprum_child_user_cpu_seconds``: Histogram of child user CPU time
    - ``cuprum_child_system_cpu_seconds``: Histogram of child system CPU time

    All metrics include ``program`` and ``project`` labels. The three resource
    histograms and their counter additionally carry a low-cardinality
    ``resource_usage_mode`` label naming their source — ``wait4_child``,
    ``aggregate_cpu_delta``, or ``unavailable`` — so a run that measures
    nothing is distinguishable from one that measured a small value. The
    histograms are observed only where the figure was actually measured; the
    counter is emitted for every recorded mode, including ``unavailable``.

    Parameters
    ----------
    collector:
        A :class:`MetricsCollector` implementation for the target backend.

    Example
    -------
    ::

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters["cuprum_executions_total"] == 1.0

    """

    __slots__ = ("_collector",)

    def __init__(self, collector: MetricsCollector) -> None:
        """Initialize the metrics hook with a collector."""
        self._collector = collector

    def __call__(self, event: ExecEvent) -> None:
        """Process an execution event and update metrics.

        The pure ``_metric_operations`` reducer decides which counters and
        histograms this event yields; the labels are resolved and applied only
        when there is at least one operation, so a ``plan`` (or a phaseless
        no-op) event never computes labels.

        Parameters
        ----------
        event : ExecEvent
            The execution event processed to derive and apply metric
            operations.

        Notes
        -----
        An ``exit`` event can yield up to six operations, applied as
        independent collector calls in a fixed order: the failure counter (only
        for a known non-zero exit code), then the duration observation (only
        when a duration was measured), then the resource counter, and finally
        the resource histograms — maximum RSS, user CPU, and system CPU, each
        only where that figure was measured. There is no atomicity across them,
        and none is attempted: the collector wraps an arbitrary backend
        (``prometheus_client``, statsd, OpenTelemetry), and this adapter cannot
        make multiple writes to such a backend transactional. Buffering them to
        apply together would only move the problem, while delaying when metrics
        appear.

        So if the collector raises part-way through, the earlier calls stay
        applied: a failure can be recorded without its duration. That is
        accepted rather than hidden. The exception then leaves this hook and is
        not swallowed: :func:`cuprum._observability._emit_exec_event` logs
        ``observe_hook_failed`` and re-raises, so a raising collector fails the
        user's command. A collector that must not do that has to swallow its
        own errors.

        Collector implementations should therefore treat each call as
        independent and ordered, and must not assume that seeing a
        ``cuprum_failures_total`` increment guarantees a matching
        ``cuprum_duration_seconds`` observation will follow.

        No event or operation identifier is passed, so a collector has nothing
        to deduplicate on and a repeated call increments again. Nothing here is
        idempotent, and this hook never retries a failed call.
        """
        operations = _metric_operations(event)
        if not operations:
            return
        labels = self._extract_labels(event)
        for operation in operations:
            self._apply(operation, labels)

    def _apply(
        self,
        operation: _MetricOp,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Apply one metric operation to the collector with the event labels."""
        # ``labels`` is the same mapping for every operation of one event, so
        # the per-operation extras are merged into a copy rather than mutating
        # it; the resource operations carry the accounting mode, all others
        # carry none.
        match operation:
            case _CounterOp(name=name, value=value, labels=extra):
                self._collector.inc_counter(name, value, {**labels, **extra})
            case _HistogramOp(name=name, value=value, labels=extra):
                self._collector.observe_histogram(name, value, {**labels, **extra})

    @staticmethod
    def _extract_labels(event: ExecEvent) -> dict[str, str]:
        """Extract low-cardinality label values from an event."""
        # Labels deliberately use only the program and project tag;
        # high-cardinality fields (pid, argv, lines) are excluded by design.
        return {
            "program": str(event.program) or "unknown",
            "project": (
                event.project
                if event.phase == "pipeline_fail_fast"
                else _project_tag(event)
            )
            or "unknown",
        }


def metrics_hook(collector: MetricsCollector) -> ExecHook:
    """Create a metrics observe hook for the given collector.

    This is a convenience factory that returns a :class:`MetricsHook` instance
    cast to the :class:`~cuprum.events.ExecHook` type.

    Parameters
    ----------
    collector:
        A :class:`MetricsCollector` implementation.

    Returns
    -------
    ExecHook
        A hook suitable for use with ``sh.observe()``.

    """
    return MetricsHook(collector)


__all__ = [
    "InMemoryMetrics",
    "MetricsCollector",
    "MetricsHook",
    "metrics_hook",
]
