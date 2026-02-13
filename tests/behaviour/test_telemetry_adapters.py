"""Behavioural tests for telemetry adapter modules.

Note on type casting
--------------------
This module uses ``typ.cast()`` when accessing pytest-bdd fixture values from
dictionaries. This pattern is necessary because pytest-bdd fixtures return
``object`` types, and the dict-based fixture approach used here (returning
``dict[str, object]``) loses type information. More specific types (e.g.
``"ExecHook"``, ``"InMemoryMetrics"``) are used when the target type is known;
``"typ.Any"`` is used otherwise. The casts reduce type safety but are confined
to test code where runtime behavior is verified by assertions.
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
from cuprum.context import ScopeConfig, scoped
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecHook

_STDOUT_STDERR_SCRIPT = "\n".join(
    (
        "import sys",
        "print('stdout-line')",
        "print('stderr-line', file=sys.stderr)",
    ),
)

_SUCCESS_SCRIPT = "print('ok')"
_FAILURE_SCRIPT = "import sys; sys.exit(1)"
_OUTPUT_SCRIPT = "\n".join(
    (
        "import sys",
        "print('traced-output')",
        "print('traced-error', file=sys.stderr)",
    ),
)


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

    with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
        result = cmd.run_sync()

    behaviour_state["result"] = result


def _run_command_with_hook(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    hook_fixture: dict[str, object],
    script: str,
) -> None:
    """Run a Python command with a hook extracted from a fixture.

    Parameters
    ----------
    behaviour_state:
        Shared mutable state dictionary.
    python_cmd_fixture:
        Python command builder fixture.
    hook_fixture:
        Fixture containing a hook under the "hook" key.
    script:
        Python script to execute with -c flag.

    """
    hook = typ.cast("ExecHook", hook_fixture["hook"])
    _execute_python_command(behaviour_state, python_cmd_fixture, hook, script)


@when("I run a command that writes to stdout and stderr")
def when_run_stdout_stderr(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    logging_hook_fixture: dict[str, object],
) -> None:
    """Run a command that writes to both streams."""
    _run_command_with_hook(
        behaviour_state, python_cmd_fixture, logging_hook_fixture, _STDOUT_STDERR_SCRIPT
    )


@when("I run a command that succeeds")
def when_run_success(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    metrics_fixture: dict[str, object],
) -> None:
    """Run a command that exits with code 0."""
    _run_command_with_hook(
        behaviour_state, python_cmd_fixture, metrics_fixture, _SUCCESS_SCRIPT
    )


@when("I run a command that fails with metrics tracking")
def when_run_failure_with_metrics(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    metrics_fixture: dict[str, object],
) -> None:
    """Run a failing command with metrics hook."""
    _run_command_with_hook(
        behaviour_state, python_cmd_fixture, metrics_fixture, _FAILURE_SCRIPT
    )


@when("I run a command that fails with tracing")
def when_run_failure_with_tracer(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    tracer_fixture: dict[str, object],
) -> None:
    """Run a failing command with tracer hook."""
    _run_command_with_hook(
        behaviour_state, python_cmd_fixture, tracer_fixture, _FAILURE_SCRIPT
    )


@when("I run a command that writes output")
def when_run_with_output(
    behaviour_state: dict[str, object],
    python_cmd_fixture: dict[str, object],
    tracer_fixture: dict[str, object],
) -> None:
    """Run a command with output for tracing."""
    _run_command_with_hook(
        behaviour_state, python_cmd_fixture, tracer_fixture, _OUTPUT_SCRIPT
    )


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
    assert hasattr(start_record, "cuprum_phase"), "Missing cuprum_phase attribute"
    assert start_record.cuprum_phase == "start", "cuprum_phase should be 'start'"
    assert hasattr(start_record, "cuprum_program"), "Missing cuprum_program attribute"


@then("the execution counter is incremented")
def then_counter_incremented(behaviour_state: dict[str, object]) -> None:
    """Verify execution counter was incremented."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    assert metrics.counters.get("cuprum_executions_total") == pytest.approx(1.0), (
        "Execution counter should be 1.0"
    )


@then("the duration histogram contains an observation")
def then_histogram_observation(behaviour_state: dict[str, object]) -> None:
    """Verify duration histogram has an observation."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    durations = metrics.histograms.get("cuprum_duration_seconds", [])
    assert len(durations) == 1, "Duration histogram should have exactly one observation"
    assert durations[0] >= 0.0, "Duration should be non-negative"


@then("the failure counter is incremented")
def then_failure_counter_incremented(behaviour_state: dict[str, object]) -> None:
    """Verify failure counter was incremented."""
    metrics = typ.cast("InMemoryMetrics", behaviour_state["metrics"])
    assert metrics.counters.get("cuprum_failures_total") == pytest.approx(1.0), (
        "Failure counter should be 1.0"
    )


@then("a span is created and ended")
def then_span_created(behaviour_state: dict[str, object]) -> None:
    """Verify a span was created and properly ended."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    assert len(tracer.spans) == 1, "Expected exactly one span"
    span = tracer.spans[0]
    assert span.ended is True, "Span should be ended"


@then("the span has program and exit code attributes")
def then_span_attributes(behaviour_state: dict[str, object]) -> None:
    """Verify span has expected attributes."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    assert "cuprum.program" in span.attributes, "Missing cuprum.program attribute"
    assert "cuprum.exit_code" in span.attributes, "Missing cuprum.exit_code attribute"
    assert span.attributes["cuprum.exit_code"] == 0, "Exit code should be 0"


@then("the span records output as events")
def then_span_events(behaviour_state: dict[str, object]) -> None:
    """Verify span has output events."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    event_names = [name for name, _ in span.events]
    assert "cuprum.stdout" in event_names, "Missing cuprum.stdout event"
    assert "cuprum.stderr" in event_names, "Missing cuprum.stderr event"


@then("the span status indicates an error")
def then_span_error_status(behaviour_state: dict[str, object]) -> None:
    """Verify span status indicates failure."""
    tracer = typ.cast("InMemoryTracer", behaviour_state["tracer"])
    span = tracer.spans[0]
    assert span.status_ok is False, "Span status should indicate error"
    assert span.attributes.get("cuprum.exit_code") == 1, "Exit code should be 1"
