"""Property and stateful tests for the metrics event-to-operation reducer.

`_metric_operations` is the pure reducer mapping each `ExecEvent` to the counter
and histogram operations the metrics hook should apply. The property tests pin
the operations produced per phase; the `RuleBasedStateMachine` drives random
event streams through a real `MetricsHook`/`InMemoryMetrics` and checks the
accumulated counters and histograms against an independent phase-count oracle —
so counters and observations are created exactly when intended, and only then.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum.adapters.metrics_adapter import (
    InMemoryMetrics,
    MetricsHook,
    _CounterOp,
    _HistogramOp,
    _metric_operations,
    _UnhandledMetricsPhaseError,
)
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    from cuprum.events import ExecPhase
    from cuprum.program import Program

_KNOWN_PHASES: list[str] = [
    "plan",
    "start",
    "stdout",
    "stderr",
    "stdin",
    "stdin_error",
    "exit",
]

_UNIT_COUNTER_PHASES = [
    ("start", "cuprum_executions_total"),
    ("stdout", "cuprum_stdout_lines_total"),
    ("stderr", "cuprum_stderr_lines_total"),
    ("stdin_error", "cuprum_stdin_errors_total"),
]


def _event(
    phase: str,
    *,
    byte_count: int | None = None,
    exit_code: int | None = None,
    duration_s: float | None = None,
) -> ExecEvent:
    """Build an ``ExecEvent`` carrying only the fields the reducer reads."""
    return ExecEvent(
        phase=typ.cast("ExecPhase", phase),
        program=typ.cast("Program", "prog"),
        argv=("prog",),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=exit_code,
        duration_s=duration_s,
        tags={},
        byte_count=byte_count,
    )


@pytest.mark.parametrize(("phase", "counter"), _UNIT_COUNTER_PHASES)
def test_unit_counter_phases_yield_one_increment(phase: str, counter: str) -> None:
    """Each simple phase yields exactly one unit increment of its counter."""
    assert _metric_operations(_event(phase)) == (_CounterOp(counter, 1.0),)


def test_plan_phase_yields_no_operations() -> None:
    """The plan phase produces no metric operations."""
    assert _metric_operations(_event("plan")) == ()


@given(byte_count=st.none() | st.integers(min_value=0, max_value=1_000_000))
def test_stdin_yields_bytes_counter_only_when_counted(
    *,
    byte_count: int | None,
) -> None:
    """A stdin event yields a bytes counter iff it carries a byte count."""
    operations = _metric_operations(_event("stdin", byte_count=byte_count))
    if byte_count is None:
        assert operations == ()
    else:
        assert operations == (
            _CounterOp("cuprum_stdin_bytes_total", float(byte_count)),
        )


@given(
    exit_code=st.none() | st.integers(min_value=-3, max_value=3),
    duration_s=st.none()
    | st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_exit_yields_failure_and_duration_only_when_present(
    *,
    exit_code: int | None,
    duration_s: float | None,
) -> None:
    """An exit event counts a failure iff non-zero, and a duration iff measured."""
    operations = _metric_operations(
        _event("exit", exit_code=exit_code, duration_s=duration_s),
    )
    expected: list[_CounterOp | _HistogramOp] = []
    if exit_code is not None and exit_code != 0:
        expected.append(_CounterOp("cuprum_failures_total", 1.0))
    if duration_s is not None:
        expected.append(_HistogramOp("cuprum_duration_seconds", duration_s))
    assert list(operations) == expected


def test_unknown_phase_raises_structured_error() -> None:
    """An out-of-contract phase raises the structured metrics error."""
    with pytest.raises(_UnhandledMetricsPhaseError):
        _metric_operations(_event("future_phase"))


@st.composite
def _events(draw: st.DrawFn) -> ExecEvent:
    """Draw an event across the known phases with phase-appropriate fields."""
    phase = draw(st.sampled_from(_KNOWN_PHASES))
    byte_count = (
        draw(st.none() | st.integers(min_value=0, max_value=1_000_000))
        if phase == "stdin"
        else None
    )
    exit_code = (
        draw(st.none() | st.integers(min_value=-3, max_value=3))
        if phase == "exit"
        else None
    )
    duration_s = (
        draw(
            st.none()
            | st.floats(
                min_value=0.0,
                max_value=100.0,
                allow_nan=False,
                allow_infinity=False,
            ),
        )
        if phase == "exit"
        else None
    )
    return _event(
        phase,
        byte_count=byte_count,
        exit_code=exit_code,
        duration_s=duration_s,
    )


class _MetricsAccumulationMachine(RuleBasedStateMachine):
    """Feed random event streams to a real hook and check accumulated metrics.

    Expectations are tracked independently of the reducer — by counting events
    per phase — so the machine cross-checks the reducer, the operation
    application, and the collector's accumulation together.
    """

    def __init__(self) -> None:
        """Start with a fresh collector, hook, and empty expectations."""
        super().__init__()
        self.metrics = InMemoryMetrics()
        self.hook = MetricsHook(self.metrics)
        self.expected_counters: dict[str, float] = {}
        self.expected_durations: list[float] = []

    def _expect_counter(self, name: str, value: float) -> None:
        """Record an independently-computed counter increment."""
        self.expected_counters[name] = self.expected_counters.get(name, 0.0) + value

    @rule(event=_events())
    def emit(self, event: ExecEvent) -> None:
        """Apply an event to the hook and to the independent expectations."""
        self.hook(event)

        phase = event.phase
        simple = {
            "start": "cuprum_executions_total",
            "stdout": "cuprum_stdout_lines_total",
            "stderr": "cuprum_stderr_lines_total",
            "stdin_error": "cuprum_stdin_errors_total",
        }
        if phase in simple:
            self._expect_counter(simple[phase], 1.0)
        elif phase == "stdin" and event.byte_count is not None:
            self._expect_counter("cuprum_stdin_bytes_total", float(event.byte_count))
        elif phase == "exit":
            if event.exit_code is not None and event.exit_code != 0:
                self._expect_counter("cuprum_failures_total", 1.0)
            if event.duration_s is not None:
                self.expected_durations.append(event.duration_s)

    @invariant()
    def collector_matches_independent_expectations(self) -> None:
        """Check the collector reflects exactly the counted per-phase effects."""
        assert self.metrics.counters == self.expected_counters
        assert (
            self.metrics.histograms.get("cuprum_duration_seconds", [])
            == self.expected_durations
        )
        # The duration histogram is the only histogram the hook ever writes.
        assert set(self.metrics.histograms) <= {"cuprum_duration_seconds"}


TestMetricsAccumulation = _MetricsAccumulationMachine.TestCase
TestMetricsAccumulation.settings = settings(
    max_examples=60,
    stateful_step_count=20,
    deadline=None,
)
