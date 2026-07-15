"""Unit tests for the metrics adapter."""

from __future__ import annotations

import logging
import sys
import typing as typ

import pytest

from cuprum import sh
from cuprum.adapters.metrics_adapter import (
    InMemoryMetrics,
    MetricsHook,
    metrics_hook,
)
from cuprum.context import ScopeConfig, scoped
from cuprum.events import ExecEvent, ExecPhase
from cuprum.program import Program
from cuprum.unittests._adapter_test_support import (
    _LabelRecordingCollector,
    _python_builder,
    _run_in_threads,
)


class TestMetricsHook:
    """Tests for MetricsHook and InMemoryMetrics."""

    @pytest.mark.parametrize(
        (
            "command_code",
            "expected_executions",
            "expected_failures",
            "failure_message",
        ),
        [
            (
                "print('hello')",
                1.0,
                0.0,
                "successful executions should increment only the executions counter",
            ),
            (
                "import sys; sys.exit(1)",
                1.0,
                1.0,
                "failed executions should increment executions and failures counters",
            ),
        ],
    )
    def test_execution_counters(
        self,
        command_code: str,
        expected_executions: float,
        expected_failures: float,
        failure_message: str,
    ) -> None:
        """Hook increments counters for successful and failed executions."""
        builder, catalogue = _python_builder(project_name="metrics-counters")
        cmd = builder("-c", command_code)

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == pytest.approx(
            expected_executions
        ), failure_message
        assert metrics.counters.get("cuprum_failures_total", 0.0) == pytest.approx(
            expected_failures
        ), failure_message

    def test_counts_output_lines(self) -> None:
        """Hook counts stdout and stderr lines."""
        builder, catalogue = _python_builder(project_name="metrics-lines")
        cmd = builder(
            "-c",
            """import sys
print('out1')
print('out2')
print('err1', file=sys.stderr)""",
        )

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_stdout_lines_total") == pytest.approx(
            2.0
        ), "stdout events should increment the stdout line counter"
        assert metrics.counters.get("cuprum_stderr_lines_total") == pytest.approx(
            1.0
        ), "stderr events should increment the stderr line counter"

    def test_records_duration_histogram(self) -> None:
        """Hook records execution duration in histogram."""
        builder, catalogue = _python_builder(project_name="metrics-duration")
        cmd = builder("-c", "print('quick')")

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        durations = metrics.histograms.get("cuprum_duration_seconds", [])
        assert len(durations) == 1, "exit events should record one duration sample"
        assert durations[0] >= 0.0, "duration samples should be non-negative"

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
                phase=typ.cast("ExecPhase", phase),
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
                # Parametrized cases supply distinct ancillary event fields.
                **typ.cast("typ.Any", extra_kwargs),
            )
        )

        assert metrics.counters.get(metric_name) == pytest.approx(expected_value), (
            f"{phase} events should update {metric_name}"
        )

    def test_factory_function_returns_hook(self) -> None:
        """metrics_hook() factory returns a valid ExecHook."""
        metrics = InMemoryMetrics()
        hook = metrics_hook(metrics)

        builder, catalogue = _python_builder()
        cmd = builder("-c", "print('factory')")

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        assert metrics.counters.get("cuprum_executions_total") == pytest.approx(1.0), (
            "metrics_hook factory should return a hook that counts executions"
        )

    def test_inmemory_metrics_reset(self) -> None:
        """InMemoryMetrics.reset() clears all metrics."""
        metrics = InMemoryMetrics()
        metrics.inc_counter("test", 1.0, {})
        metrics.observe_histogram("test_hist", 0.5, {})

        assert metrics.counters.get("test") == pytest.approx(1.0), (
            "in-memory metrics should record counters before reset"
        )
        assert len(metrics.histograms.get("test_hist", [])) == 1, (
            "in-memory metrics should record histograms before reset"
        )

        metrics.reset()

        assert metrics.counters == {}, "reset should clear in-memory counters"
        assert metrics.histograms == {}, "reset should clear in-memory histograms"

    def test_unhandled_phase_does_not_project_labels(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Unhandled phases log without touching event label fields."""

        class UnstringableProgram:
            """Program-like value that fails if metrics tries to stringify it."""

            def __str__(self) -> str:
                """Raise when unexpected label extraction stringifies the program."""
                msg = "unhandled phases must not project metrics labels"
                raise AssertionError(msg)

        metrics = InMemoryMetrics()
        hook = MetricsHook(metrics)
        caplog.set_level(logging.DEBUG, logger="cuprum.adapters")
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

        assert metrics.counters == {}, "unhandled phases should not mutate counters"
        assert metrics.histograms == {}, "unhandled phases should not mutate histograms"
        assert "Ignoring unhandled metrics adapter phase: plan" in caplog.messages, (
            "unhandled metrics phases should use the shared debug log"
        )

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

        assert metrics.counters == {}, "concurrent reset should leave counters empty"
        assert metrics.histograms == {}, (
            "concurrent reset should leave histograms empty"
        )

    def test_passes_program_and_project_labels(self) -> None:
        """MetricsHook passes correct labels to collector."""
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        builder, catalogue = _python_builder(project_name="label-test")
        cmd = builder("-c", "print('x')")

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            cmd.run_sync()

        # Verify at least one call was made with correct labels
        assert recorder.calls, "metrics hook should record labelled metric calls"

        # Check execution counter has correct labels
        exec_calls = [c for c in recorder.calls if c[0] == "cuprum_executions_total"]
        assert len(exec_calls) == 1, "execution counter should be emitted exactly once"
        _, _, labels = exec_calls[0]
        assert labels["program"] == sys.executable, (
            "execution counter should label the executed program"
        )
        assert labels["project"] == "label-test", (
            "execution counter should label the project name"
        )

        # Check duration histogram has correct labels
        duration_calls = [
            c for c in recorder.calls if c[0] == "cuprum_duration_seconds"
        ]
        assert len(duration_calls) == 1, (
            "duration histogram should be emitted exactly once"
        )
        _, _, labels = duration_calls[0]
        assert labels["program"] == sys.executable, (
            "duration histogram should label the executed program"
        )
        assert labels["project"] == "label-test", (
            "duration histogram should label the project name"
        )

    def test_project_label_treats_explicit_none_as_unknown(self) -> None:
        """MetricsHook treats an explicit ``None`` project tag as missing."""
        recorder = _LabelRecordingCollector(record_histograms=False)
        hook = MetricsHook(recorder)
        event = ExecEvent(
            phase="start",
            program=Program("tool"),
            argv=("tool",),
            cwd=None,
            env=None,
            pid=None,
            timestamp=0.0,
            line=None,
            exit_code=None,
            duration_s=None,
            tags={"project": None},
        )

        hook(event)

        assert recorder.labels == [{"program": "tool", "project": "unknown"}], (
            "explicit None project tags should use the stable unknown label"
        )
