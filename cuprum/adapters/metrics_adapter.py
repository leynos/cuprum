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
import types
import typing as typ

from cuprum.adapters._support import (
    _LockedStore,
    _project_tag,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


class _UnhandledMetricsPhaseError(ValueError):
    """Raised when metrics receive a phase outside the known event contract."""

    def __init__(self, phase: object) -> None:
        """Capture the unsupported phase and initialise its diagnostic."""
        self.phase = phase
        msg = f"Unhandled metrics phase: {phase}"
        super().__init__(msg)


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


@dc.dataclass(frozen=True, slots=True)
class _CounterOp:
    """A counter increment the metrics hook intends to apply."""

    name: str
    value: float


@dc.dataclass(frozen=True, slots=True)
class _HistogramOp:
    """A histogram observation the metrics hook intends to apply."""

    name: str
    value: float


type _MetricOp = _CounterOp | _HistogramOp

# Phases that map to a single unit-counter increment, keyed by event phase.
# Read-only, so the single source of truth for these metric names cannot be
# rewritten at runtime by an importing module.
_PHASE_COUNTERS: cabc.Mapping[str, str] = types.MappingProxyType({
    "start": "cuprum_executions_total",
    "stdout": "cuprum_stdout_lines_total",
    "stderr": "cuprum_stderr_lines_total",
    "stdin_error": "cuprum_stdin_errors_total",
})


def _exit_operations(event: ExecEvent) -> tuple[_MetricOp, ...]:
    """Return the failure counter and duration histogram ops for an exit event."""
    operations: list[_MetricOp] = []
    # A failure counter is produced only for a known non-zero exit code, and
    # a duration observation only when a duration was measured; a clean exit
    # with no duration therefore produces nothing.
    if event.exit_code is not None and event.exit_code != 0:
        operations.append(_CounterOp("cuprum_failures_total", 1.0))
    if event.duration_s is not None:
        operations.append(_HistogramOp("cuprum_duration_seconds", event.duration_s))
    return tuple(operations)


def _metric_operations(event: ExecEvent) -> tuple[_MetricOp, ...]:
    """Map an execution event to the metric operations it should produce."""
    # The pure event-to-operation reducer behind ``MetricsHook.__call__``: the
    # single source of truth for which counters and histogram observations each
    # phase yields, so the operations can be verified without a collector.
    # Labels are applied by the caller.
    phase = event.phase
    # ``plan`` yields nothing; a ``stdin`` event without a byte count yields
    # nothing; an unknown phase is a contract violation and raises
    # ``_UnhandledMetricsPhaseError``.
    match phase:
        case "plan":
            return ()
        case "stdin":
            if event.byte_count is None:
                return ()
            return (_CounterOp("cuprum_stdin_bytes_total", float(event.byte_count)),)
        case "exit":
            return _exit_operations(event)
        case _ if (counter_name := _PHASE_COUNTERS.get(phase)) is not None:
            # The unit-counter phases stay keyed by `_PHASE_COUNTERS` rather
            # than repeated as a literal alternation, so the metric names have
            # exactly one definition.
            return (_CounterOp(counter_name, 1.0),)
        case _:
            raise _UnhandledMetricsPhaseError(phase)


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
        An ``exit`` event can yield two operations — a failure counter and a
        duration observation — applied as two independent collector calls, in
        that order. There is no atomicity across them, and none is attempted:
        the collector wraps an arbitrary backend (``prometheus_client``,
        statsd, OpenTelemetry), and this adapter cannot make two writes to such
        a backend transactional. Buffering them to apply together would only
        move the problem, while delaying when metrics appear.

        So if the collector raises on the second call, the first stays applied:
        a failure can be recorded without its duration. That is accepted rather
        than hidden. The exception then leaves this hook and is not swallowed:
        :func:`cuprum._observability._emit_exec_event` logs
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
        match operation:
            case _CounterOp(name=name, value=value):
                self._collector.inc_counter(name, value, labels)
            case _HistogramOp(name=name, value=value):
                self._collector.observe_histogram(name, value, labels)

    @staticmethod
    def _extract_labels(event: ExecEvent) -> dict[str, str]:
        """Extract low-cardinality label values from an event."""
        # Labels deliberately use only the program and project tag;
        # high-cardinality fields (pid, argv, lines) are excluded by design.
        return {
            "program": str(event.program) or "unknown",
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
