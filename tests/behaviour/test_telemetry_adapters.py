"""Behavioural tests for telemetry adapter modules.

Note on type casting
--------------------
This module uses ``typ.cast("typ.Any", ...)`` extensively when accessing
pytest-bdd fixture values from dictionaries. This pattern is necessary because
pytest-bdd fixtures return ``object`` types, and the dict-based fixture approach
used here (returning ``dict[str, object]``) loses type information. The casts
reduce type safety but are confined to test code where runtime behavior is
verified by assertions.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import sh
from cuprum.adapters.logging_adapter import structured_logging_hook
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook
from cuprum.adapters.tracing_adapter import InMemoryTracer, TracingHook
from cuprum.context import scoped
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook


@scenario(
    "../features/telemetry_adapters.feature",
    "Structured logging hook emits structured records",
)
def test_structured_logging_hook_emits_records() -> None:
    """Behavioural coverage for structured logging adapter."""


@scenario(
    "../features/telemetry_adapters.feature",
    "Metrics hook collects execution counters and histograms",
)
def test_metrics_hook_collects_counters_histograms() -> None:
    """Behavioural coverage for metrics adapter success path."""


@scenario(
    "../features/telemetry_adapters.feature",
    "Metrics hook tracks failure counts",
)
def test_metrics_hook_tracks_failures() -> None:
    """Behavioural coverage for metrics adapter failure path."""


@scenario(
    "../features/telemetry_adapters.feature",
    "Tracing hook creates spans with attributes",
)
def test_tracing_hook_creates_spans() -> None:
    """Behavioural coverage for tracing adapter success path."""


@scenario(
    "../features/telemetry_adapters.feature",
    "Tracing hook sets error status on failure",
)
def test_tracing_hook_error_status() -> None:
    """Behavioural coverage for tracing adapter failure path."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given("a curated Python command for testing", target_fixture="python_cmd_fixture")
def given_python_command() -> dict[str, object]:
    """Set up a Python command builder and catalogue."""
    catalogue, python_program = python_catalogue()
    return {
        "catalogue": catalogue,
        "python_program": python_program,
        "builder": sh.make(python_program, catalogue=catalogue),
    }


@given(
    "a structured logging hook with a test logger",
    target_fixture="logging_hook_fixture",
)
def given_structured_logging_hook(
    behaviour_state: dict[str, object],
) -> dict[str, object]:
    """Set up a structured logging hook with a dedicated test logger."""
    logger = logging.getLogger("cuprum.test.adapters.bdd")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    class RecordCapture(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.records: list[logging.LogRecord] = []

        def emit(self, record: logging.LogRecord) -> None:
            self.records.append(record)

    handler = RecordCapture()
    logger.addHandler(handler)

    hook = structured_logging_hook(logger=logger)
    behaviour_state["log_handler"] = handler

    return {"logger": logger, "hook": hook, "handler": handler}


@given("an in-memory metrics collector", target_fixture="metrics_fixture")
def given_metrics_collector(behaviour_state: dict[str, object]) -> dict[str, object]:
    """Set up an in-memory metrics collector."""
    metrics = InMemoryMetrics()
    hook = MetricsHook(metrics)
    behaviour_state["metrics"] = metrics
    return {"metrics": metrics, "hook": hook}


@given("an in-memory tracer", target_fixture="tracer_fixture")
def given_tracer(behaviour_state: dict[str, object]) -> dict[str, object]:
    """Set up an in-memory tracer."""
    tracer = InMemoryTracer()
    hook = TracingHook(tracer, record_output=True)
    behaviour_state["tracer"] = tracer
    return {"tracer": tracer, "hook": hook}


def _execute_python_command(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    hook: ExecHook,
    script: str,
) -> None:
    """Execute a Python command with the given hook and store the result.

    This helper extracts catalogue and builder from python_cmd_fixture, builds
    a command with the given script, runs it inside scoped/observe contexts,
    and stores the result in behaviour_state.
    """
    catalogue = typ.cast("typ.Any", python_cmd_fixture["catalogue"])
    builder = typ.cast("typ.Any", python_cmd_fixture["builder"])
    cmd = builder("-c", script)

    with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
        result = cmd.run_sync()

    behaviour_state["result"] = result


@when("I run a command that writes to stdout and stderr")
def when_run_stdout_stderr(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    logging_hook_fixture: dict[str, object],
) -> None:
    """Run a command that writes to both streams."""
    hook = typ.cast("ExecHook", logging_hook_fixture["hook"])
    script = "\n".join(
        (
            "import sys",
            "print('stdout-line')",
            "print('stderr-line', file=sys.stderr)",
        ),
    )
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, script)


@when("I run a command that succeeds")
def when_run_success(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    metrics_fixture: dict[str, object],
) -> None:
    """Run a command that exits with code 0."""
    hook = typ.cast("ExecHook", metrics_fixture["hook"])
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, "print('ok')")


@when("I run a command that fails with metrics tracking")
def when_run_failure_with_metrics(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    metrics_fixture: dict[str, object],
) -> None:
    """Run a failing command with metrics hook."""
    hook = typ.cast("ExecHook", metrics_fixture["hook"])
    _execute_python_command(
        behaviour_state, python_cmd_fixture, hook, "import sys; sys.exit(1)"
    )


@when("I run a command that fails with tracing")
def when_run_failure_with_tracer(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    tracer_fixture: dict[str, object],
) -> None:
    """Run a failing command with tracer hook."""
    hook = typ.cast("ExecHook", tracer_fixture["hook"])
    _execute_python_command(
        behaviour_state, python_cmd_fixture, hook, "import sys; sys.exit(1)"
    )


@when("I run a command that writes output")
def when_run_with_output(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    tracer_fixture: dict[str, object],
) -> None:
    """Run a command with output for tracing."""
    hook = typ.cast("ExecHook", tracer_fixture["hook"])
    script = "\n".join(
        (
            "import sys",
            "print('traced-output')",
            "print('traced-error', file=sys.stderr)",
        ),
    )
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, script)


@then("the logger receives records for all execution phases")
def then_all_phases_logged(
    behaviour_state: dict[str, object],
    logging_hook_fixture: dict[str, object],
) -> None:
    """Verify all execution phases are logged."""
    handler = typ.cast("typ.Any", logging_hook_fixture["handler"])
    records = handler.records

    messages = [r.message for r in records]
    assert any("cuprum.plan" in m for m in messages), "Missing plan event"
    assert any("cuprum.start" in m for m in messages), "Missing start event"
    assert any("cuprum.stdout" in m for m in messages), "Missing stdout event"
    assert any("cuprum.stderr" in m for m in messages), "Missing stderr event"
    assert any("cuprum.exit" in m for m in messages), "Missing exit event"


@then("each record contains cuprum-prefixed extra fields")
def then_extra_fields_present(
    logging_hook_fixture: dict[str, object],
) -> None:
    """Verify extra fields are attached to log records."""
    handler = typ.cast("typ.Any", logging_hook_fixture["handler"])
    records = handler.records

    start_record = next((r for r in records if "cuprum.start" in r.message), None)
    assert start_record is not None, "No start record found in log records"
    assert hasattr(start_record, "cuprum_phase")
    assert start_record.cuprum_phase == "start"
    assert hasattr(start_record, "cuprum_program")


@then("the execution counter is incremented")
def then_counter_incremented(behaviour_state: dict[str, object]) -> None:
    """Verify execution counter was incremented."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    assert metrics.counters.get("cuprum_executions_total") == 1.0


@then("the duration histogram contains an observation")
def then_histogram_observation(behaviour_state: dict[str, object]) -> None:
    """Verify duration histogram has an observation."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    durations = metrics.histograms.get("cuprum_duration_seconds", [])
    assert len(durations) == 1
    assert durations[0] >= 0.0


@then("the failure counter is incremented")
def then_failure_counter_incremented(behaviour_state: dict[str, object]) -> None:
    """Verify failure counter was incremented."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    assert metrics.counters.get("cuprum_failures_total") == 1.0


@then("a span is created and ended")
def then_span_created(behaviour_state: dict[str, object]) -> None:
    """Verify a span was created and properly ended."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    assert len(tracer.spans) == 1
    span = tracer.spans[0]
    assert span.ended is True


@then("the span has program and exit code attributes")
def then_span_attributes(behaviour_state: dict[str, object]) -> None:
    """Verify span has expected attributes."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    assert "cuprum.program" in span.attributes
    assert "cuprum.exit_code" in span.attributes
    assert span.attributes["cuprum.exit_code"] == 0


@then("the span records output as events")
def then_span_events(behaviour_state: dict[str, object]) -> None:
    """Verify span has output events."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    event_names = [name for name, _ in span.events]
    assert "cuprum.stdout" in event_names
    assert "cuprum.stderr" in event_names


@then("the span status indicates an error")
def then_span_error_status(behaviour_state: dict[str, object]) -> None:
    """Verify span status indicates failure."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    assert span.status_ok is False
    assert span.attributes.get("cuprum.exit_code") == 1
