"""Unit tests for the tracing adapter."""

from __future__ import annotations

from cuprum import sh
from cuprum.adapters.tracing_adapter import (
    InMemorySpan,
    InMemoryTracer,
    TracingHook,
    tracing_hook,
)
from cuprum.context import ScopeConfig, scoped
from cuprum.unittests._adapter_test_support import _python_builder, _run_in_threads


class TestTracingHook:
    """Tests for TracingHook and InMemoryTracer."""

    def _run_traced_command(
        self,
        *,
        project_name: str,
        command_code: str,
        record_output: bool = True,
    ) -> tuple[InMemoryTracer, InMemorySpan]:
        """Run a Python command with tracing and return the tracer and first span.

        Args:
            project_name: Name for the project settings.
            command_code: Python code to execute via `-c` flag.
            record_output: Whether to record stdout/stderr as span events.

        Returns
        -------
            Tuple of (tracer, first_span) for assertions in the calling test.

        """
        builder, catalogue = _python_builder(project_name=project_name)
        cmd = builder("-c", command_code)

        tracer = InMemoryTracer()
        hook = TracingHook(tracer, record_output=record_output)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        return tracer, tracer.spans[0]

    def test_creates_span_for_execution(self) -> None:
        """Hook creates a span from start to exit."""
        builder, catalogue = _python_builder(project_name="tracing-span")
        cmd = builder("-c", "print('traced')")

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert len(tracer.spans) == 1
        span = tracer.spans[0]
        assert span.name.startswith("cuprum.exec")
        assert span.ended is True
        assert "cuprum.program" in span.attributes
        assert "cuprum.pid" in span.attributes

    def test_sets_exit_attributes(self) -> None:
        """Hook sets exit_code and duration_s on span."""
        _, span = self._run_traced_command(
            project_name="tracing-exit",
            command_code="print('done')",
        )

        assert span.attributes.get("cuprum.exit_code") == 0
        assert span.attributes.get("cuprum.duration_s") is not None
        assert span.status_ok is True

    def test_sets_error_status_on_failure(self) -> None:
        """Hook sets error status on non-zero exit."""
        _, span = self._run_traced_command(
            project_name="tracing-error",
            command_code="import sys; sys.exit(42)",
        )

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

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
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
        _, span = self._run_traced_command(
            project_name="tracing-no-output",
            command_code="print('skip')",
            record_output=False,
        )

        assert len(span.events) == 0

    def test_includes_project_tag(self) -> None:
        """Hook includes project tag in span attributes."""
        _, span = self._run_traced_command(
            project_name="my-project",
            command_code="print('x')",
        )

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

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            pipeline.run_sync()

        assert len(tracer.spans) == 2
        assert all(span.ended for span in tracer.spans)

    def test_factory_function_returns_hook(self) -> None:
        """tracing_hook() factory returns a valid ExecHook."""
        tracer = InMemoryTracer()
        hook = tracing_hook(tracer, record_output=False)

        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('factory')")

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
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

    def test_concurrent_tracer_reset_leaves_valid_empty_state(self) -> None:
        """Concurrent reset calls keep the in-memory tracer store coherent."""
        tracer = InMemoryTracer()
        iterations = 100

        def start_and_reset() -> None:
            """Exercise span creation and reset under lock contention."""
            for index in range(iterations):
                span = tracer.start_span(f"test-{index}")
                span.end()
                tracer.reset()

        _run_in_threads(start_and_reset)

        tracer.reset()

        assert tracer.spans == []

    def test_pid_less_events_do_not_create_spans(self) -> None:
        """Events without a pid are ignored by the tracing hook."""
        from cuprum.events import ExecEvent
        from cuprum.program import Program

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        # Create a mock event with pid=None
        event = ExecEvent(
            phase="start",
            program=Program("echo"),
            argv=("echo", "hello"),
            cwd=None,
            env=None,
            pid=None,
            timestamp=0.0,
            line=None,
            exit_code=None,
            duration_s=None,
            tags={},
        )

        # Call the hook for start, output, and exit phases
        hook(event)

        output_event = ExecEvent(
            phase="stdout",
            program=Program("echo"),
            argv=("echo", "hello"),
            cwd=None,
            env=None,
            pid=None,
            timestamp=0.0,
            line="hello",
            exit_code=None,
            duration_s=None,
            tags={},
        )
        hook(output_event)

        exit_event = ExecEvent(
            phase="exit",
            program=Program("echo"),
            argv=("echo", "hello"),
            cwd=None,
            env=None,
            pid=None,
            timestamp=0.0,
            line=None,
            exit_code=0,
            duration_s=0.1,
            tags={},
        )
        hook(exit_event)

        # No spans should have been created
        assert tracer.spans == []

    def test_pipeline_attributes_are_set_on_spans(self) -> None:
        """Pipeline-related tags are included in span attributes."""
        from cuprum.events import ExecEvent
        from cuprum.program import Program

        tracer = InMemoryTracer()
        hook = TracingHook(tracer)

        # Create a mock event with pipeline tags
        start_event = ExecEvent(
            phase="start",
            program=Program("cat"),
            argv=("cat",),
            cwd=None,
            env=None,
            pid=1234,
            timestamp=0.0,
            line=None,
            exit_code=None,
            duration_s=None,
            tags={
                "project": "pipeline-test",
                "pipeline_stage_index": 1,
                "pipeline_stages": 3,
            },
        )
        hook(start_event)

        # End the span
        exit_event = ExecEvent(
            phase="exit",
            program=Program("cat"),
            argv=("cat",),
            cwd=None,
            env=None,
            pid=1234,
            timestamp=0.0,
            line=None,
            exit_code=0,
            duration_s=0.1,
            tags={
                "project": "pipeline-test",
                "pipeline_stage_index": 1,
                "pipeline_stages": 3,
            },
        )
        hook(exit_event)

        assert len(tracer.spans) == 1
        span = tracer.spans[0]
        assert span.attributes.get("cuprum.pipeline_stage_index") == 1
        assert span.attributes.get("cuprum.pipeline_stages") == 3
        assert span.attributes.get("cuprum.project") == "pipeline-test"
