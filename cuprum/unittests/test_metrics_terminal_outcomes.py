"""Metric adapter projections for settled execution outcomes."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.adapters._metrics_operations import _CounterOp, _HistogramOp
from cuprum.adapters.metrics_adapter import (
    MetricsHook,
    _exit_operations,
    _metric_operations,
)
from cuprum.context import EnvMode
from cuprum.events import ResourceUsageMode, TerminalOutcome
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    _make_exec_event,
)


@given(
    exit_code=st.none() | st.integers(min_value=-3, max_value=3),
    duration_s=st.none()
    | st.floats(min_value=0.0, max_value=100.0, allow_nan=False, allow_infinity=False),
)
def test_exit_does_not_duplicate_terminal_metrics(
    *,
    exit_code: int | None,
    duration_s: float | None,
) -> None:
    """Failure and duration metrics belong to settlement, not process exit."""
    operations = _metric_operations(
        _make_exec_event(
            phase="exit",
            overrides={"exit_code": exit_code, "duration_s": duration_s},
        )
    )

    assert not any(
        operation.name in {"cuprum_failures_total", "cuprum_duration_seconds"}
        for operation in operations
    ), f"exit must not duplicate terminal metrics, found {operations!r}"


@pytest.mark.parametrize("outcome", list(TerminalOutcome))
def test_settled_records_category_failure_and_duration(
    outcome: TerminalOutcome,
) -> None:
    """Settlement records a bounded category and measured duration."""
    recorder = _LabelRecordingCollector()
    hook = MetricsHook(recorder)
    hook(
        _make_exec_event(
            phase="settled",
            overrides={
                "terminal_outcome": outcome,
                "duration_s": 0.25,
                "tags": {"project": "terminal-metrics"},
            },
        )
    )

    expected = [
        (
            "cuprum_terminal_outcomes_total",
            1.0,
            {
                "program": "cat",
                "project": "terminal-metrics",
                "terminal_outcome": str(outcome),
            },
        )
    ]
    if outcome is not TerminalOutcome.EXIT_ZERO:
        expected.append((
            "cuprum_failures_total",
            1.0,
            {"program": "cat", "project": "terminal-metrics"},
        ))
    expected.append((
        "cuprum_duration_seconds",
        0.25,
        {"program": "cat", "project": "terminal-metrics"},
    ))

    assert recorder.calls == expected, (
        f"settled metric projection differs: {recorder.calls!r}"
    )


def test_legacy_exit_operations_keep_failure_duration_and_resource_projection() -> None:
    """Direct users of the old helper retain its former operation tuple."""
    event = _make_exec_event(
        phase="exit",
        overrides={
            "exit_code": 7,
            "duration_s": 0.5,
            "max_rss_bytes": 2048,
            "user_cpu_seconds": 0.25,
            "system_cpu_seconds": 0.125,
            "resource_usage_mode": ResourceUsageMode.WAIT4_CHILD,
            "env_mode": EnvMode.REPLACE,
        },
    )

    assert _exit_operations(event) == (
        _CounterOp("cuprum_failures_total", 1.0, {"env_mode": "replace"}),
        _HistogramOp("cuprum_duration_seconds", 0.5),
        _CounterOp(
            "cuprum_resource_usage_measurements_total",
            1.0,
            {"resource_usage_mode": "wait4_child"},
        ),
        _HistogramOp(
            "cuprum_child_max_rss_bytes",
            2048.0,
            {"resource_usage_mode": "wait4_child"},
        ),
        _HistogramOp(
            "cuprum_child_user_cpu_seconds",
            0.25,
            {"resource_usage_mode": "wait4_child"},
        ),
        _HistogramOp(
            "cuprum_child_system_cpu_seconds",
            0.125,
            {"resource_usage_mode": "wait4_child"},
        ),
    ), "the compatibility helper must preserve its previous exit projection"
