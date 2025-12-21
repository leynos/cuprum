"""Unit tests for telemetry adapter modules."""

# ruff: noqa: PLR6301,SIM117

from __future__ import annotations

import logging
import sys
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.adapters.logging_adapter import (
    JsonLoggingFormatter,
    structured_logging_hook,
)
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook, metrics_hook
from cuprum.adapters.tracing_adapter import (
    InMemoryTracer,
    TracingHook,
    tracing_hook,
)
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import scoped
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import pytest


def _python_builder(
    *, project_name: str = "adapter-tests"
) -> tuple[typ.Callable[..., sh.SafeCmd], ProgramCatalogue]:
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name=project_name,
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    return sh.make(python_program, catalogue=catalogue), catalogue


class TestStructuredLoggingHook:
    """Tests for structured_logging_hook."""

    def test_logs_all_phases(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook logs plan, start, stdout, stderr, and exit events."""
        builder, catalogue = _python_builder(project_name="logging-test")
        cmd = builder(
            "-c",
            "\n".join(
                (
                    "import sys",
                    "print('out1')",
                    "print('err1', file=sys.stderr)",
                ),
            ),
        )

        logger = logging.getLogger("cuprum.exec.test")
        logger.setLevel(logging.DEBUG)
        hook = structured_logging_hook(logger=logger)

        with caplog.at_level(logging.DEBUG, logger="cuprum.exec.test"):
            with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
                result = cmd.run_sync()

        assert result.exit_code == 0

        messages = [r.message for r in caplog.records]
        assert any("cuprum.plan" in m for m in messages)
        assert any("cuprum.start" in m for m in messages)
        assert any("cuprum.stdout" in m and "out1" in m for m in messages)
        assert any("cuprum.stderr" in m and "err1" in m for m in messages)
        assert any("cuprum.exit" in m for m in messages)

    def test_includes_extra_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook attaches cuprum_* extra fields to log records."""
        builder, catalogue = _python_builder(project_name="extra-fields")
        cmd = builder("-c", "print('hello')")

        logger = logging.getLogger("cuprum.exec.extras")
        logger.setLevel(logging.DEBUG)
        hook = structured_logging_hook(logger=logger)

        with caplog.at_level(logging.DEBUG, logger="cuprum.exec.extras"):
            with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
                cmd.run_sync()

        start_record = next(r for r in caplog.records if "cuprum.start" in r.message)
        assert hasattr(start_record, "cuprum_phase")
        assert start_record.cuprum_phase == "start"
        assert hasattr(start_record, "cuprum_program")
        assert hasattr(start_record, "cuprum_pid")

    def test_respects_log_levels(self, caplog: pytest.LogCaptureFixture) -> None:
        """Hook respects configured log levels for each phase."""
        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('x')")

        logger = logging.getLogger("cuprum.exec.levels")
        logger.setLevel(logging.INFO)
        hook = structured_logging_hook(
            logger=logger,
            plan_level=logging.DEBUG,
            start_level=logging.INFO,
            output_level=logging.DEBUG,
            exit_level=logging.WARNING,
        )

        with caplog.at_level(logging.INFO, logger="cuprum.exec.levels"):
            with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
                cmd.run_sync()

        messages = [r.message for r in caplog.records]
        assert not any("cuprum.plan" in m for m in messages)
        assert any("cuprum.start" in m for m in messages)
        assert not any("cuprum.stdout" in m for m in messages)
        assert any("cuprum.exit" in m for m in messages)


class TestJsonLoggingFormatter:
    """Tests for JsonLoggingFormatter."""

    def test_formats_as_json(self) -> None:
        """Formatter outputs valid JSON with structured fields."""
        import json

        formatter = JsonLoggingFormatter()
        record = logging.LogRecord(
            name="cuprum.exec",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="cuprum.start program=echo",
            args=(),
            exc_info=None,
        )
        record.cuprum_phase = "start"
        record.cuprum_program = "echo"
        record.cuprum_pid = 12345

        output = formatter.format(record)
        data = json.loads(output)

        assert data["level"] == "INFO"
        assert data["logger"] == "cuprum.exec"
        assert data["cuprum_phase"] == "start"
        assert data["cuprum_program"] == "echo"
        assert data["cuprum_pid"] == 12345


class TestMetricsHook:
    """Tests for MetricsHook and InMemoryMetrics."""

    def test_increments_execution_counter(self) -> None:
        """Hook increments cuprum_executions_total on start."""
        builder, catalogue = _python_builder(project_name="metrics-counter")
        cmd = builder("-c", "print('hello')")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == 1.0

    def test_counts_output_lines(self) -> None:
        """Hook counts stdout and stderr lines."""
        builder, catalogue = _python_builder(project_name="metrics-lines")
        cmd = builder(
            "-c",
            "\n".join(
                (
                    "import sys",
                    "print('out1')",
                    "print('out2')",
                    "print('err1', file=sys.stderr)",
                ),
            ),
        )

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_stdout_lines_total") == 2.0
        assert metrics.counters.get("cuprum_stderr_lines_total") == 1.0

    def test_records_duration_histogram(self) -> None:
        """Hook records execution duration in histogram."""
        builder, catalogue = _python_builder(project_name="metrics-duration")
        cmd = builder("-c", "print('quick')")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        durations = metrics.histograms.get("cuprum_duration_seconds", [])
        assert len(durations) == 1
        assert durations[0] >= 0.0

    def test_counts_failures(self) -> None:
        """Hook increments failure counter on non-zero exit."""
        builder, catalogue = _python_builder(project_name="metrics-failures")
        cmd = builder("-c", "import sys; sys.exit(1)")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_failures_total") == 1.0

    def test_factory_function_returns_hook(self) -> None:
        """metrics_hook() factory returns a valid ExecHook."""
        metrics = InMemoryMetrics()
        hook = metrics_hook(metrics)

        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('factory')")

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == 1.0

    def test_inmemory_metrics_reset(self) -> None:
        """InMemoryMetrics.reset() clears all metrics."""
        metrics = InMemoryMetrics()
        metrics.inc_counter("test", 1.0, {})
        metrics.observe_histogram("test_hist", 0.5, {})

        assert metrics.counters.get("test") == 1.0
        assert len(metrics.histograms.get("test_hist", [])) == 1

        metrics.reset()

        assert metrics.counters == {}
        assert metrics.histograms == {}


class TestTracingHook:
    """Tests for TracingHook and InMemoryTracer."""

    def test_creates_span_for_execution(self) -> None:
        """Hook creates a span from start to exit."""
        builder, catalogue = _python_builder(project_name="tracing-span")
        cmd = builder("-c", "print('traced')")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert len(tracer.spans) == 1
        span = tracer.spans[0]
        assert span.name.startswith("cuprum.exec")
        assert span.ended is True
        assert "cuprum.program" in span.attributes
        assert "cuprum.pid" in span.attributes

    def test_sets_exit_attributes(self) -> None:
        """Hook sets exit_code and duration_s on span."""
        builder, catalogue = _python_builder(project_name="tracing-exit")
        cmd = builder("-c", "print('done')")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        span = tracer.spans[0]
        assert span.attributes.get("cuprum.exit_code") == 0
        assert span.attributes.get("cuprum.duration_s") is not None
        assert span.status_ok is True

    def test_sets_error_status_on_failure(self) -> None:
        """Hook sets error status on non-zero exit."""
        builder, catalogue = _python_builder(project_name="tracing-error")
        cmd = builder("-c", "import sys; sys.exit(42)")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        span = tracer.spans[0]
        assert span.attributes.get("cuprum.exit_code") == 42
        assert span.status_ok is False

    def test_records_output_events(self) -> None:
        """Hook records stdout/stderr as span events."""
        builder, catalogue = _python_builder(project_name="tracing-output")
        cmd = builder(
            "-c",
            "\n".join(
                (
                    "import sys",
                    "print('stdout-line')",
                    "print('stderr-line', file=sys.stderr)",
                ),
            ),
        )

        tracer = InMemoryTracer()
        hook = TracingHook(tracer, record_output=True)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        span = tracer.spans[0]
        event_names = [name for name, _ in span.events]
        assert "cuprum.stdout" in event_names
        assert "cuprum.stderr" in event_names

        stdout_event = next(
            (attrs for name, attrs in span.events if name == "cuprum.stdout"),
            None,
        )
        assert stdout_event is not None
        assert stdout_event.get("line") == "stdout-line"

    def test_disables_output_recording(self) -> None:
        """Hook skips output events when record_output=False."""
        builder, catalogue = _python_builder(project_name="tracing-no-output")
        cmd = builder("-c", "print('skip')")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer, record_output=False)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        span = tracer.spans[0]
        assert len(span.events) == 0

    def test_includes_project_tag(self) -> None:
        """Hook includes project tag in span attributes."""
        builder, catalogue = _python_builder(project_name="my-project")
        cmd = builder("-c", "print('x')")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        span = tracer.spans[0]
        assert span.attributes.get("cuprum.project") == "my-project"

    def test_pipeline_creates_multiple_spans(self) -> None:
        """Hook creates separate spans for each pipeline stage."""
        builder, catalogue = _python_builder(project_name="tracing-pipeline")
        stage1 = builder("-c", "print('hello')")
        stage2 = builder(
            "-c",
            "\n".join(
                (
                    "import sys",
                    "data = sys.stdin.read()",
                    "sys.stdout.write(data.upper())",
                ),
            ),
        )
        pipeline = stage1 | stage2

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            pipeline.run_sync()

        assert len(tracer.spans) == 2
        assert all(span.ended for span in tracer.spans)

    def test_factory_function_returns_hook(self) -> None:
        """tracing_hook() factory returns a valid ExecHook."""
        tracer = InMemoryTracer()
        hook = tracing_hook(tracer, record_output=False)

        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('factory')")

        with scoped(allowlist=catalogue.allowlist), sh.observe(hook):
            cmd.run_sync()

        assert len(tracer.spans) == 1

    def test_inmemory_tracer_reset(self) -> None:
        """InMemoryTracer.reset() clears all spans."""
        tracer = InMemoryTracer()
        span = tracer.start_span("test")
        span.end()

        assert len(tracer.spans) == 1

        tracer.reset()

        assert tracer.spans == []
