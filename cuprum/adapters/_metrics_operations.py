"""Metric-operation planning for the Prometheus-style metrics adapter.

This module holds the pure event-to-operation reducer behind
:class:`~cuprum.adapters.metrics_adapter.MetricsHook`: the single source of
truth for which counters and histogram observations each execution-event
phase yields. Keeping this planning logic separate from the collector-facing
API in :mod:`cuprum.adapters.metrics_adapter` lets the mapping be exercised
and verified without a collector, and keeps each module within the
project's line-count limit.
"""

from __future__ import annotations

import dataclasses as dc
import types
import typing as typ

from cuprum.events import TerminalOutcome

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent


class _UnhandledMetricsPhaseError(ValueError):
    """Raised when metrics receive a phase outside the known event contract."""

    def __init__(self, phase: object) -> None:
        """Capture the unsupported phase and initialize its diagnostic."""
        self.phase = phase
        msg = f"Unhandled metrics phase: {phase}"
        super().__init__(msg)


@dc.dataclass(frozen=True, slots=True)
class _CounterOp:
    """A counter increment the metrics hook intends to apply."""

    name: str
    value: float
    #: Extra labels merged over the event's common ones. Only the resource
    #: operations set this: the accounting mode is meaningful for the resource
    #: metrics and would be an ``unknown``-valued column on every other one.
    labels: cabc.Mapping[str, str] = dc.field(default_factory=dict)


@dc.dataclass(frozen=True, slots=True)
class _HistogramOp:
    """A histogram observation the metrics hook intends to apply."""

    name: str
    value: float
    #: Extra labels, as on :class:`_CounterOp`.
    labels: cabc.Mapping[str, str] = dc.field(default_factory=dict)


type _MetricOp = _CounterOp | _HistogramOp

# Phases that map to a single unit-counter increment, keyed by event phase.
# Read-only, so the single source of truth for these metric names cannot be
# rewritten at runtime by an importing module.
_PHASE_COUNTERS: cabc.Mapping[str, str] = types.MappingProxyType({
    "start": "cuprum_executions_total",
    "stdout": "cuprum_stdout_lines_total",
    "stderr": "cuprum_stderr_lines_total",
    "stdin_error": "cuprum_stdin_errors_total",
    "stdio_error": "cuprum_stdio_errors_total",
    "timeout": "cuprum_timeouts_total",
    "teardown_error": "cuprum_teardown_errors_total",
    "capture_eof_grace_expired": "cuprum_capture_eof_grace_expired_total",
    "pipeline_fail_fast": "cuprum_pipeline_fail_fast_total",
    "settled": "cuprum_terminal_outcomes_total",
})

# The unit-counter phases whose counter describes the execution as a whole and
# therefore carries the effective environment mode. The stream, drain, and
# pipe counters deliberately do not: their volume is per-line or per-event, and
# multiplying every one of those series to repeat a per-execution constant
# would buy nothing an operator cannot read off ``cuprum_executions_total``.
_ENV_MODE_PHASES: frozenset[str] = frozenset({"start"})


def _env_mode_label(event: ExecEvent) -> dict[str, str]:
    """Return the ``env_mode`` label for an event that records a policy.

    A bounded label by construction: :class:`~cuprum.context.env_overlay.EnvMode`
    is a closed three-value ``StrEnum`` resolved by production code, so no
    caller-supplied string can reach it. Absent rather than invented when an
    event carries no mode, so a hand-built event does not fabricate one.

    Returns
    -------
    dict[str, str]
        The ``env_mode`` label, or an empty mapping when the event records no
        mode, so callers may merge it unconditionally.
    """
    if event.env_mode is None:
        return {}
    # Rendered, not passed through, for the same reason as the resource mode
    # below: the label must be the plain string an operator filters on rather
    # than the member's ``repr``.
    return {"env_mode": str(event.env_mode)}


def _resource_operations(event: ExecEvent) -> tuple[_MetricOp, ...]:
    """Return the resource-measurement ops for a terminal ``exit`` event."""
    operations: list[_MetricOp] = []
    mode = event.resource_usage_mode
    if mode is None:
        return ()
    # Rendered, not passed through: the mode is a ``StrEnum``, and the label
    # must reach the collector as the plain string that appears in the series
    # an operator filters on rather than as the member's ``repr``.
    labels = {"resource_usage_mode": str(mode)}
    # The counter is emitted whenever a mode is recorded, the ``unavailable``
    # case included: that it is worth counting at all is the one signal a bare
    # absence of resource samples cannot carry.
    operations.append(
        _CounterOp("cuprum_resource_usage_measurements_total", 1.0, labels)
    )
    # Each figure yields a histogram only where it was actually measured, so an
    # unmeasured platform contributes no samples rather than a stream of zeros
    # that would drag every percentile toward it.
    if event.max_rss_bytes is not None:
        operations.append(
            _HistogramOp(
                "cuprum_child_max_rss_bytes",
                float(event.max_rss_bytes),
                labels,
            )
        )
    if event.user_cpu_seconds is not None:
        operations.append(
            _HistogramOp(
                "cuprum_child_user_cpu_seconds",
                event.user_cpu_seconds,
                labels,
            )
        )
    if event.system_cpu_seconds is not None:
        operations.append(
            _HistogramOp(
                "cuprum_child_system_cpu_seconds",
                event.system_cpu_seconds,
                labels,
            )
        )
    return tuple(operations)


def _exit_operations(event: ExecEvent) -> tuple[_MetricOp, ...]:
    """Preserve the former private exit projection for direct importers."""
    operations: list[_MetricOp] = []
    if event.exit_code is not None and event.exit_code != 0:
        operations.append(
            _CounterOp("cuprum_failures_total", 1.0, _env_mode_label(event))
        )
    if event.duration_s is not None:
        operations.append(_HistogramOp("cuprum_duration_seconds", event.duration_s))
    return (*operations, *_resource_operations(event))


def _settled_operations(event: ExecEvent) -> tuple[_MetricOp, ...]:
    """Return category, failure, and duration ops for a settled event."""
    outcome = event.terminal_outcome
    if outcome is None:
        return ()

    operations: list[_MetricOp] = [
        _CounterOp(
            "cuprum_terminal_outcomes_total",
            1.0,
            {"terminal_outcome": str(outcome)},
        )
    ]
    if outcome is not TerminalOutcome.EXIT_ZERO:
        operations.append(
            _CounterOp("cuprum_failures_total", 1.0, _env_mode_label(event))
        )
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
            # Duration and failure metrics now come from ``settled``. The
            # existing ``exit`` event remains the resource-measurement carrier.
            return _resource_operations(event)
        case "settled":
            return _settled_operations(event)
        case "stdio_error":
            # A per-operation label rather than a fourth common one: the
            # boundary category belongs only on this counter, and adding it to
            # ``_extract_labels`` would attach it to every series the hook
            # emits, where it is ``None`` for all but this phase. The metric
            # name still comes from ``_PHASE_COUNTERS``, so it has one
            # definition like the other unit counters.
            labels = (
                {}
                if event.error_category is None
                else {"error_category": str(event.error_category)}
            )
            return (_CounterOp("cuprum_stdio_errors_total", 1.0, labels),)
        case _ if (counter_name := _PHASE_COUNTERS.get(phase)) is not None:
            # The unit-counter phases stay keyed by `_PHASE_COUNTERS` rather
            # than repeated as a literal alternation, so the metric names have
            # exactly one definition.
            labels = _env_mode_label(event) if phase in _ENV_MODE_PHASES else {}
            return (_CounterOp(counter_name, 1.0, labels),)
        case _:
            raise _UnhandledMetricsPhaseError(phase)


__all__ = [
    "_PHASE_COUNTERS",
    "_CounterOp",
    "_HistogramOp",
    "_MetricOp",
    "_UnhandledMetricsPhaseError",
    "_exit_operations",
    "_metric_operations",
    "_resource_operations",
]
