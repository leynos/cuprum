"""Property and stateful tests for the metrics event-to-operation reducer.

`_metric_operations` is the pure reducer mapping each `ExecEvent` to the counter
and histogram operations the metrics hook should apply. The property tests pin
the operations produced per phase; the `RuleBasedStateMachine` drives random
event streams through a real `MetricsHook`/`InMemoryMetrics` and checks the
accumulated counters and histograms against an independent phase-count oracle —
so counters and observations are created exactly when intended, and only then.
"""

from __future__ import annotations

import types
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from cuprum import sh
from cuprum.adapters.metrics_adapter import (
    InMemoryMetrics,
    MetricsHook,
    _CounterOp,
    _HistogramOp,
    _metric_operations,
    _UnhandledMetricsPhaseError,
)
from cuprum.context import ScopeConfig, scoped
from cuprum.events import ExecEvent
from cuprum.unittests._adapter_test_support import _python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecPhase
    from cuprum.program import Program

_KNOWN_PHASES: list[str] = [
    "plan",
    "start",
    "stdout",
    "stderr",
    "stdin",
    "stdin_error",
    "timeout",
    "teardown_error",
    "capture_eof_grace_expired",
    "exit",
    "pipeline_fail_fast",
]

_UNIT_COUNTER_PHASES = (
    ("start", "cuprum_executions_total"),
    ("stdout", "cuprum_stdout_lines_total"),
    ("stderr", "cuprum_stderr_lines_total"),
    ("stdin_error", "cuprum_stdin_errors_total"),
    ("timeout", "cuprum_timeouts_total"),
    ("teardown_error", "cuprum_teardown_errors_total"),
    ("capture_eof_grace_expired", "cuprum_capture_eof_grace_expired_total"),
    ("pipeline_fail_fast", "cuprum_pipeline_fail_fast_total"),
)

# The same pairs keyed for lookup, so the stateful oracle and the parametrized
# cases cannot drift apart. This stays a *test-local* restatement of the
# mapping rather than an import of the adapter's `_PHASE_COUNTERS`: an oracle
# that read the production table would agree with it by construction and could
# not catch a wrong metric name.
_UNIT_COUNTERS: cabc.Mapping[str, str] = types.MappingProxyType(
    dict(_UNIT_COUNTER_PHASES)
)


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
    assert _metric_operations(_event(phase)) == (_CounterOp(counter, 1.0),), (
        f"phase {phase!r} must yield exactly one unit increment of {counter!r}"
    )


def test_plan_phase_yields_no_operations() -> None:
    """The plan phase produces no metric operations."""
    assert _metric_operations(_event("plan")) == (), (
        "the plan phase precedes execution, so it must yield no operations"
    )


@given(byte_count=st.none() | st.integers(min_value=0, max_value=1_000_000))
def test_stdin_yields_bytes_counter_only_when_counted(
    *,
    byte_count: int | None,
) -> None:
    """A stdin event yields a bytes counter iff it carries a byte count."""
    operations = _metric_operations(_event("stdin", byte_count=byte_count))
    if byte_count is None:
        assert operations == (), (
            f"an uncounted stdin event must yield no operations, found {operations!r}"
        )
    else:
        assert operations == (
            _CounterOp("cuprum_stdin_bytes_total", float(byte_count)),
        ), (
            f"a stdin event carrying {byte_count} bytes must yield exactly that "
            f"counter, found {operations!r}"
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
    assert list(operations) == expected, (
        f"exit(exit_code={exit_code!r}, duration_s={duration_s!r}) must yield "
        f"{expected!r}, found {list(operations)!r}"
    )


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

        match event.phase:
            case _ if (counter := _UNIT_COUNTERS.get(event.phase)) is not None:
                self._expect_counter(counter, 1.0)
            case "stdin" if event.byte_count is not None:
                self._expect_counter(
                    "cuprum_stdin_bytes_total",
                    float(event.byte_count),
                )
            case "exit":
                if event.exit_code is not None and event.exit_code != 0:
                    self._expect_counter("cuprum_failures_total", 1.0)
                if event.duration_s is not None:
                    self.expected_durations.append(event.duration_s)
            case _:
                # `plan` records nothing, and a `stdin` event carrying no byte
                # count contributes no bytes. Both leave the oracle unchanged.
                pass

    @invariant()
    def collector_matches_independent_expectations(self) -> None:
        """Check the collector reflects exactly the counted per-phase effects."""
        assert self.metrics.counters == self.expected_counters, (
            "collected counters must match the independent phase-count oracle; "
            f"collected {self.metrics.counters!r}, expected "
            f"{self.expected_counters!r}"
        )
        observed_durations = self.metrics.histograms.get("cuprum_duration_seconds", [])
        assert observed_durations == self.expected_durations, (
            "the duration histogram must hold exactly the measured durations, in "
            f"order; observed {observed_durations!r}, expected "
            f"{self.expected_durations!r}"
        )
        # The duration histogram is the only histogram the hook ever writes.
        assert set(self.metrics.histograms) <= {"cuprum_duration_seconds"}, (
            "the hook must write no histogram beyond cuprum_duration_seconds, "
            f"found {sorted(self.metrics.histograms)}"
        )


TestMetricsAccumulation = _MetricsAccumulationMachine.TestCase
TestMetricsAccumulation.settings = settings(
    max_examples=60,
    stateful_step_count=20,
    deadline=None,
)


class _MetricsBackendError(Exception):
    """Test exception standing in for a backend rejecting a metric write."""


class _FailingHistogramCollector(InMemoryMetrics):
    """A collector whose histogram writes always fail.

    Stands in for a metrics backend that is reachable for one call and not the
    next — the case that decides what a partially-applied event leaves behind.
    """

    @typ.override
    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Fail as a backend rejecting the observation would."""
        msg = f"metrics backend rejected {name}"
        raise _MetricsBackendError(msg)


def test_a_failing_second_operation_leaves_the_first_applied() -> None:
    """A partly-applied exit event keeps what already landed, and reports.

    An exit event yields a failure counter then a duration observation, as two
    independent collector calls. There is no atomicity across them and none is
    attempted, so this pins what a caller actually observes rather than leaving
    it to chance: the counter stays, the histogram is absent, and the backend's
    own exception leaves the hook unwrapped rather than being absorbed.
    """
    collector = _FailingHistogramCollector()
    hook = MetricsHook(collector)
    event = _event("exit", exit_code=3, duration_s=1.5)

    with pytest.raises(_MetricsBackendError, match="rejected cuprum_duration_seconds"):
        hook(event)

    assert collector.counters == {"cuprum_failures_total": 1.0}, (
        "the counter applied before the failure must remain recorded, found "
        f"{collector.counters!r}"
    )
    assert collector.histograms == {}, (
        "the observation that raised must not be recorded, found "
        f"{collector.histograms!r}"
    )


def test_a_failing_collector_fails_the_command() -> None:
    """A raising collector is not isolated; its exception fails the command.

    Cuprum logs ``observe_hook_failed`` and then re-raises, so the backend's
    own exception type reaches the caller of ``run_sync`` unchanged. This pins
    the escalation path the adapter documents, and is why the reducer's
    fail-closed phase match matters: an unhandled phase would take commands
    down for every caller that has registered the hook.
    """
    builder, catalogue = _python_builder(project_name="metrics-failure")
    cmd = builder("-c", "pass")
    hook = MetricsHook(_FailingHistogramCollector())

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
        pytest.raises(_MetricsBackendError, match="rejected cuprum_duration_seconds"),
    ):
        cmd.run_sync()
