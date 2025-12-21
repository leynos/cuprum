"""Prometheus-style metrics adapter for Cuprum execution events.

This module provides an observe hook that collects metrics about command
execution in a format compatible with Prometheus client libraries. The
adapter demonstrates how to collect:

- **Counters**: Total executions, failures, output lines
- **Histograms**: Execution duration distribution

The implementation uses protocol classes to remain decoupled from specific
metrics libraries. Projects can implement the protocols with their preferred
backend (prometheus_client, statsd, OpenTelemetry metrics, etc.).

Example with the in-memory reference implementation::

    from cuprum import scoped, sh
    from cuprum.adapters.metrics_adapter import MetricsHook, InMemoryMetrics

    metrics = InMemoryMetrics()

    with scoped(allowlist=my_allowlist), sh.observe(MetricsHook(metrics)):
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
import threading
import typing as typ

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
        ...

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
        ...


@dc.dataclass
class InMemoryMetrics:
    """Reference in-memory metrics collector for testing and examples.

    This collector stores metrics in memory for inspection. It is thread-safe
    and suitable for unit testing but not for production use.

    Attributes
    ----------
    counters:
        Dict mapping metric names to accumulated counter values.
    histograms:
        Dict mapping metric names to lists of observed values.

    """

    counters: dict[str, float] = dc.field(default_factory=dict)
    histograms: dict[str, list[float]] = dc.field(default_factory=dict)
    _lock: threading.Lock = dc.field(default_factory=threading.Lock, repr=False)

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

    def reset(self) -> None:
        """Clear all collected metrics."""
        with self._lock:
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

    All metrics include ``program`` and ``project`` labels.

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
        """Process an execution event and update metrics."""
        labels = self._extract_labels(event)

        match event.phase:
            case "start":
                self._collector.inc_counter(
                    "cuprum_executions_total",
                    1.0,
                    labels,
                )
            case "stdout":
                self._collector.inc_counter(
                    "cuprum_stdout_lines_total",
                    1.0,
                    labels,
                )
            case "stderr":
                self._collector.inc_counter(
                    "cuprum_stderr_lines_total",
                    1.0,
                    labels,
                )
            case "exit":
                if event.exit_code is not None and event.exit_code != 0:
                    self._collector.inc_counter(
                        "cuprum_failures_total",
                        1.0,
                        labels,
                    )
                if event.duration_s is not None:
                    self._collector.observe_histogram(
                        "cuprum_duration_seconds",
                        event.duration_s,
                        labels,
                    )

    @staticmethod
    def _extract_labels(event: ExecEvent) -> dict[str, str]:
        """Extract label values from an event."""
        project = str(event.tags.get("project", "unknown"))
        return {
            "program": str(event.program),
            "project": project,
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
