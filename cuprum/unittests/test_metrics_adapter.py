"""Unit tests for the metrics adapter."""

from __future__ import annotations

import sys
import typing as typ

import pytest

from cuprum import sh
from cuprum.adapters.metrics_adapter import InMemoryMetrics, MetricsHook, metrics_hook
from cuprum.context import ScopeConfig, scoped
from cuprum.events import ExecEvent
from cuprum.program import Program
from cuprum.unittests._adapter_test_support import _python_builder, _run_in_threads


class TestMetricsHook:
    """Tests for MetricsHook and InMemoryMetrics."""

    def test_increments_execution_counter(self) -> None:
        """Hook increments cuprum_executions_total on start."""
        builder, catalogue = _python_builder(project_name="metrics-counter")
        cmd = builder("-c", "print('hello')")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == pytest.approx(1.0)

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

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_stdout_lines_total") == pytest.approx(2.0)
        assert metrics.counters.get("cuprum_stderr_lines_total") == pytest.approx(1.0)

    def test_records_duration_histogram(self) -> None:
        """Hook records execution duration in histogram."""
        builder, catalogue = _python_builder(project_name="metrics-duration")
        cmd = builder("-c", "print('quick')")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
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

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_failures_total") == pytest.approx(1.0)

    @pytest.mark.parametrize(
        ("phase", "extra_kwargs", "metric_name", "expected_value"),
        [
            (
                "stdin_error",
                {"note": "BrokenPipeError: forced EPIPE"},
                "cuprum_stdin_errors_total",
                1.0,
            ),
            ("stdin", {"byte_count": 7}, "cuprum_stdin_bytes_total", 7.0),
        ],
    )
    def test_counts_stdin_metrics(
        self,
        phase: str,
        extra_kwargs: dict[str, object],
        metric_name: str,
        expected_value: float,
    ) -> None:
        """Hook increments stdin byte and error counters for stdin events."""
        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)
        program = Program(sys.executable)

        hook(
            ExecEvent(
                phase=typ.cast("typ.Any", phase),
                program=program,
                argv=(str(program), "-c", "pass"),
                cwd=None,
                env=None,
                pid=123,
                timestamp=0.0,
                line=None,
                exit_code=None,
                duration_s=None,
                tags={"project": "stdin-metrics"},
                **typ.cast("typ.Any", extra_kwargs),
            )
        )

        assert metrics.counters.get(metric_name) == pytest.approx(expected_value)

    def test_factory_function_returns_hook(self) -> None:
        """metrics_hook() factory returns a valid ExecHook."""
        metrics = InMemoryMetrics()
        hook = metrics_hook(metrics)

        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('factory')")

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == pytest.approx(1.0)

    def test_inmemory_metrics_reset(self) -> None:
        """InMemoryMetrics.reset() clears all metrics."""
        metrics = InMemoryMetrics()
        metrics.inc_counter("test", 1.0, {})
        metrics.observe_histogram("test_hist", 0.5, {})

        assert metrics.counters.get("test") == pytest.approx(1.0)
        assert len(metrics.histograms.get("test_hist", [])) == 1

        metrics.reset()

        assert metrics.counters == {}
        assert metrics.histograms == {}

    def test_unhandled_phase_does_not_project_labels(self) -> None:
        """Unhandled phases return before touching event label fields."""

        class UnstringableProgram:
            """Program-like value that fails if metrics tries to stringify it."""

            def __str__(self) -> str:
                """Raise when unexpected label extraction stringifies the program."""
                msg = "unhandled phases must not project metrics labels"
                raise AssertionError(msg)

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)
        event = ExecEvent(
            phase="plan",
            program=typ.cast("Program", UnstringableProgram()),
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

        hook(event)

        assert metrics.counters == {}
        assert metrics.histograms == {}

    def test_concurrent_metrics_reset_leaves_valid_empty_state(self) -> None:
        """Concurrent reset calls keep the in-memory metrics store coherent."""
        metrics = InMemoryMetrics()
        iterations = 100

        def mutate_and_reset() -> None:
            """Exercise collector mutators and reset under lock contention."""
            for index in range(iterations):
                metrics.inc_counter("test", 1.0, {})
                metrics.observe_histogram("test_hist", float(index), {})
                metrics.reset()

        _run_in_threads(mutate_and_reset)

        metrics.reset()

        assert metrics.counters == {}
        assert metrics.histograms == {}

    def test_passes_program_and_project_labels(self) -> None:
        """MetricsHook passes correct labels to collector."""

        class LabelRecordingCollector:
            """Collector that records metric name, value, and labels."""

            def __init__(self) -> None:
                """Initialise an empty list to record metric calls."""
                self.calls: list[tuple[str, float, dict[str, str]]] = []

            def inc_counter(
                self,
                name: str,
                value: float,
                labels: dict[str, str],
            ) -> None:
                """Record a counter increment with its name, value, and labels."""
                self.calls.append((name, value, dict(labels)))

            def observe_histogram(
                self,
                name: str,
                value: float,
                labels: dict[str, str],
            ) -> None:
                """Record a histogram observation with its name, value, and labels."""
                self.calls.append((name, value, dict(labels)))

        collector = LabelRecordingCollector()
        hook = MetricsHook(collector)  # type: ignore[arg-type]

        builder, catalogue = _python_builder(project_name="label-test")
        cmd = builder("-c", "print('x')")

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        # Verify at least one call was made with correct labels
        assert len(collector.calls) > 0

        # Check execution counter has correct labels
        exec_calls = [c for c in collector.calls if c[0] == "cuprum_executions_total"]
        assert len(exec_calls) == 1
        _, _, labels = exec_calls[0]
        assert labels["program"] == sys.executable
        assert labels["project"] == "label-test"

        # Check duration histogram has correct labels
        duration_calls = [
            c for c in collector.calls if c[0] == "cuprum_duration_seconds"
        ]
        assert len(duration_calls) == 1
        _, _, labels = duration_calls[0]
        assert labels["program"] == sys.executable
        assert labels["project"] == "label-test"
