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

from cuprum.adapters._support import (
    _event_common_fields,
    _LockedStore,
    _project_tag,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook

# Phases the metrics hook translates into collector updates; other phases
# (for example ``plan``) deliberately record nothing.
_HANDLED_PHASES: frozenset[str] = frozenset(
    {"start", "stdout", "stderr", "stdin", "stdin_error", "exit"},
)


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

    Storage and locking follow the shared :class:`~cuprum.adapters._support.
    _LockedStore` contract: every mutator holds the lock, and ``reset()``
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
        # Defer label extraction until the phase is known to be handled so
        # unhandled phases (for example ``plan``) do no projection work.
        if event.phase not in _HANDLED_PHASES:
            return
        labels = self._extract_labels(event)

        match event.phase:
            case "start":
                self._increment("cuprum_executions_total", labels=labels)
            case "stdout":
                self._increment("cuprum_stdout_lines_total", labels=labels)
            case "stderr":
                self._increment("cuprum_stderr_lines_total", labels=labels)
            case "stdin_error":
                self._increment("cuprum_stdin_errors_total", labels=labels)
            case "stdin":
                self._record_stdin_bytes(event, labels=labels)
            case "exit":
                self._record_exit(event, labels=labels)
            case _:
                # Unreachable behind the _HANDLED_PHASES guard; kept so a new
                # phase added to the guard without a handler fails loudly in
                # review rather than silently matching nothing.
                pass

    def _increment(
        self,
        name: str,
        *,
        labels: cabc.Mapping[str, str],
        value: float = 1.0,
    ) -> None:
        """Increment a counter with the current event labels."""
        self._collector.inc_counter(name, value, labels)

    def _record_stdin_bytes(
        self,
        event: ExecEvent,
        *,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record stdin byte throughput when the event carries a byte count."""
        if event.byte_count is not None:
            self._increment(
                "cuprum_stdin_bytes_total",
                value=float(event.byte_count),
                labels=labels,
            )

    def _record_exit(
        self,
        event: ExecEvent,
        *,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record exit-code and duration metrics for an exit event."""
        if event.exit_code is not None and event.exit_code != 0:
            self._increment("cuprum_failures_total", labels=labels)
        if event.duration_s is not None:
            self._collector.observe_histogram(
                "cuprum_duration_seconds",
                event.duration_s,
                labels,
            )

    @staticmethod
    def _extract_labels(event: ExecEvent) -> dict[str, str]:
        """Extract low-cardinality label values from an event.

        Labels deliberately use only the canonical ``program`` projection plus
        the ``project`` tag; high-cardinality fields (pid, argv, lines) are
        excluded by design.
        """
        common = dict(_event_common_fields(event, lambda field: field))
        return {
            "program": str(common["program"]),
            "project": _project_tag(event) or "unknown",
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
