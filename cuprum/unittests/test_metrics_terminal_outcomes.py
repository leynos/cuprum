"""Metric adapter projections for settled execution outcomes."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.adapters.metrics_adapter import MetricsHook, _metric_operations
from cuprum.events import TerminalOutcome
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

    assert recorder.calls == expected
