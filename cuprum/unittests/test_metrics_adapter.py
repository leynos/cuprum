"""Unit tests for the metrics adapter."""

from __future__ import annotations

import sys
import threading
import typing as typ

import pytest

from cuprum.adapters.metrics_adapter import (
    InMemoryMetrics,
    MetricsHook,
    _UnhandledMetricsPhaseError,
)
from cuprum.adapters.metrics_adapter import metrics_hook as make_metrics_hook
from cuprum.events import ExecEvent, ExecPhase
from cuprum.program import Program
from cuprum.unittests._adapter_test_support import (
    Metered,
    _LabelRecordingCollector,
    _make_exec_event,
    _run_observed_python,
    metrics_hook,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["metrics_hook"]


class TestMetricsHook:
    """Tests for MetricsHook and InMemoryMetrics."""

    @pytest.mark.parametrize(
        ("command_code", "expected_counts", "failure_message"),
        [
            (
                "print('hello')",
                (1.0, 0.0),
                "successful executions should increment only the executions counter",
            ),
            (
                "import sys; sys.exit(1)",
                (1.0, 1.0),
                "failed executions should increment executions and failures counters",
            ),
        ],
    )
    def test_execution_counters(
        self,
        metrics_hook: Metered,
        command_code: str,
        expected_counts: tuple[float, float],
        failure_message: str,
    ) -> None:
        """Hook increments counters for successful and failed executions."""
        expected_executions, expected_failures = expected_counts
        metrics, hook = metrics_hook
        _run_observed_python(hook, "-c", command_code, project_name="metrics-counters")

        assert metrics.counters.get("cuprum_executions_total") == pytest.approx(
            expected_executions
        ), failure_message
        assert metrics.counters.get("cuprum_failures_total", 0.0) == pytest.approx(
            expected_failures
        ), failure_message

    def test_counts_output_lines(self, metrics_hook: Metered) -> None:
        """Hook counts stdout and stderr lines."""
        metrics, hook = metrics_hook
        _run_observed_python(
            hook,
            "-c",
            """import sys
print('out1')
print('out2')
print('err1', file=sys.stderr)""",
            project_name="metrics-lines",
        )

        assert metrics.counters.get("cuprum_stdout_lines_total") == pytest.approx(
            2.0
        ), "stdout events should increment the stdout line counter"
        assert metrics.counters.get("cuprum_stderr_lines_total") == pytest.approx(
            1.0
        ), "stderr events should increment the stderr line counter"

    def test_records_duration_histogram(self, metrics_hook: Metered) -> None:
        """Hook records execution duration in histogram."""
        metrics, hook = metrics_hook
        _run_observed_python(
            hook, "-c", "print('quick')", project_name="metrics-duration"
        )

        durations = metrics.histograms.get("cuprum_duration_seconds", [])
        assert len(durations) == 1, "exit events should record one duration sample"
        assert durations[0] >= 0.0, "duration samples should be non-negative"

    @pytest.mark.parametrize(
        ("phase", "extra_kwargs", "expected_counter"),
        [
            (
                "stdin_error",
                {"note": "BrokenPipeError: forced EPIPE"},
                ("cuprum_stdin_errors_total", 1.0),
            ),
            ("stdin", {"byte_count": 7}, ("cuprum_stdin_bytes_total", 7.0)),
        ],
    )
    def test_counts_stdin_metrics(
        self,
        metrics_hook: Metered,
        phase: str,
        extra_kwargs: dict[str, object],
        expected_counter: tuple[str, float],
    ) -> None:
        """Hook increments stdin byte and error counters for stdin events."""
        metric_name, expected_value = expected_counter
        metrics, hook = metrics_hook
        program = Program(sys.executable)

        hook(
            _make_exec_event(
                phase=typ.cast("ExecPhase", phase),
                overrides={
                    "program": str(program),
                    "argv": (str(program), "-c", "pass"),
                    "pid": 123,
                    "tags": {"project": "stdin-metrics"},
                    # Parametrized cases supply distinct ancillary event fields.
                    **extra_kwargs,
                },
            )
        )

        assert metrics.counters == {metric_name: expected_value}, (
            f"{phase} events should update only {metric_name}"
        )

    @pytest.mark.parametrize(
        ("phase", "extra_kwargs", "metric_name"),
        [
            (
                "timeout",
                {
                    "operation": "wait",
                    "timeout_s": 1.5,
                    "timeout_mode": "elapsed_deadline",
                },
                "cuprum_timeouts_total",
            ),
            (
                "teardown_error",
                {"operation": "drain", "error_type": "ValueError"},
                "cuprum_teardown_errors_total",
            ),
            (
                "capture_eof_grace_expired",
                {
                    "operation": "drain",
                    "eof_grace_s": 0.25,
                    "pending_readers": 1,
                },
                "cuprum_capture_eof_grace_expired_total",
            ),
        ],
    )
    def test_counts_timeout_metrics(
        self,
        metrics_hook: Metered,
        phase: str,
        extra_kwargs: dict[str, object],
        metric_name: str,
    ) -> None:
        """Hook counts timeout, drain failure, and capture-grace diagnostics."""
        metrics, hook = metrics_hook

        hook(
            _make_exec_event(
                phase=typ.cast("ExecPhase", phase),
                overrides={
                    "pid": 321,
                    "tags": {"project": "timeout-metrics"},
                    **extra_kwargs,
                },
            )
        )

        assert metrics.counters == {metric_name: 1.0}, (
            f"{phase} events should update only {metric_name}"
        )
        assert metrics.histograms == {}, (
            f"{phase} is a counter-only ancillary phase and must not observe a "
            f"histogram, got {metrics.histograms!r}"
        )

    def test_capture_eof_grace_metric_uses_only_standard_labels(self) -> None:
        """Grace expiry increments once without introducing diagnostic labels."""
        collector = _LabelRecordingCollector()
        hook = MetricsHook(collector)

        hook(
            _make_exec_event(
                phase="capture_eof_grace_expired",
                overrides={
                    "operation": "drain",
                    "eof_grace_s": 0.25,
                    "pending_readers": 2,
                    "pid": 321,
                    "tags": {"project": "grace-metrics", "untrusted": "value"},
                },
            )
        )

        assert collector.calls == [
            (
                "cuprum_capture_eof_grace_expired_total",
                1.0,
                {"program": "cat", "project": "grace-metrics"},
            )
        ], "grace expiry must increment once using only program/project labels"

    def test_factory_function_returns_hook(self) -> None:
        """metrics_hook() factory returns a valid ExecHook."""
        metrics = InMemoryMetrics()

        _run_observed_python(make_metrics_hook(metrics), "-c", "print('factory')")

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

        assert not metrics.counters, "reset should clear in-memory counters"
        assert not metrics.histograms, "reset should clear in-memory histograms"

    def test_plan_phase_does_not_project_labels(self, metrics_hook: Metered) -> None:
        """The plan phase is a no-op without touching event label fields."""

        class UnstringableProgram:
            """Program-like value that fails if metrics tries to stringify it."""

            def __str__(self) -> str:
                """Raise when unexpected label extraction stringifies the program."""
                msg = "unhandled phases must not project metrics labels"
                raise AssertionError(msg)

        metrics, hook = metrics_hook
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

        assert metrics.counters == {}, "plan events should not mutate counters"
        assert metrics.histograms == {}, "plan events should not mutate histograms"

    def test_unknown_phase_raises_structured_error(
        self,
        metrics_hook: Metered,
    ) -> None:
        """Unknown phases expose their value through a structured error."""
        metrics, hook = metrics_hook

        with pytest.raises(_UnhandledMetricsPhaseError) as exc_info:
            hook(_make_exec_event(phase=typ.cast("ExecPhase", "future_phase")))

        assert exc_info.value.phase == "future_phase", (
            "unknown metrics phases should remain inspectable"
        )
        assert "future_phase" in str(exc_info.value), (
            "unknown metrics errors should include the offending phase"
        )
        assert metrics.counters == {}, "unknown phases should not mutate counters"
        assert metrics.histograms == {}, "unknown phases should not mutate histograms"

    def test_concurrent_metrics_reset_leaves_valid_empty_state(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reset excludes concurrent metrics mutations until clearing completes."""
        metrics = InMemoryMetrics()
        reset_started = threading.Event()
        release_reset = threading.Event()
        mutation_started = threading.Event()
        mutation_finished = threading.Event()
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        original_clear = InMemoryMetrics._clear

        def block_clear(store: InMemoryMetrics) -> None:
            """Pause reset after it acquires the collector lock."""
            reset_started.set()
            assert release_reset.wait(timeout=1.0), "test should release reset"
            original_clear(store)

        def mutate() -> None:
            """Attempt to write state while reset is paused."""
            mutation_started.set()
            metrics.inc_counter("after", 1.0, {})
            metrics.observe_histogram("after", 1.0, {})
            mutation_finished.set()

        def record_failure(target: cabc.Callable[[], None]) -> None:
            """Run a worker and retain any failure for the main thread."""
            try:
                target()
            except BaseException as exc:  # ruff: ignore[blind-except] - worker failures must surface.
                with errors_lock:
                    errors.append(exc)

        monkeypatch.setattr(InMemoryMetrics, "_clear", block_clear)
        reset_thread = threading.Thread(
            target=lambda: record_failure(metrics.reset), daemon=True
        )
        mutation_thread = threading.Thread(
            target=lambda: record_failure(mutate), daemon=True
        )
        reset_thread.start()
        assert reset_started.wait(timeout=1.0), "reset should reach its clear step"
        mutation_thread.start()
        assert mutation_started.wait(timeout=1.0), "mutation should attempt to run"
        assert not mutation_finished.wait(timeout=0.1), (
            "metrics mutations must wait until reset releases the shared lock"
        )

        release_reset.set()
        reset_thread.join(timeout=5.0)
        mutation_thread.join(timeout=5.0)

        assert not reset_thread.is_alive(), "reset worker should finish"
        assert not mutation_thread.is_alive(), "mutation worker should finish"
        if errors:
            raise errors[0]
        assert metrics.counters == {"after": 1.0}, (
            "post-reset counter mutation should survive the atomic clear"
        )
        assert metrics.histograms == {"after": [1.0]}, (
            "post-reset histogram mutation should survive the atomic clear"
        )

    def test_passes_program_and_project_labels(self) -> None:
        """MetricsHook passes correct labels to collector."""
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        _run_observed_python(hook, "-c", "print('x')", project_name="label-test")

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
        event = _make_exec_event(
            phase="start",
            overrides={"program": "tool", "tags": {"project": None}},
        )

        hook(event)

        assert recorder.labels == [{"program": "tool", "project": "unknown"}], (
            "explicit None project tags should use the stable unknown label"
        )


class TestResourceMetricSurface:
    """Terminal child-resource figures and the mode that names their source."""

    @staticmethod
    def _exit_event(**overrides: object) -> ExecEvent:
        """Build an exit event with the resource fields under test.

        A real terminal event always carries an exit code and an elapsed
        duration, so the shared factory's unset defaults are filled in here
        rather than left for each test to restate.

        Returns
        -------
        ExecEvent
            An ``exit`` event carrying the supplied overrides.
        """
        return _make_exec_event(
            phase="exit",
            overrides={"exit_code": 0, "duration_s": 0.25, **overrides},
        )

    def test_attributable_measurement_emits_every_resource_metric(self) -> None:
        """A wait4 child reports RSS, both CPU figures, and their mode."""
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        hook(
            self._exit_event(
                max_rss_bytes=4_194_304,
                user_cpu_seconds=0.375,
                system_cpu_seconds=0.125,
                resource_usage_mode="wait4_child",
                tags={"project": "resource-metrics"},
            )
        )

        assert recorder.calls == [
            (
                "cuprum_duration_seconds",
                0.25,
                {
                    "program": "cat",
                    "project": "resource-metrics",
                },
            ),
            (
                "cuprum_resource_usage_measurements_total",
                1.0,
                {
                    "program": "cat",
                    "project": "resource-metrics",
                    "resource_usage_mode": "wait4_child",
                },
            ),
            (
                "cuprum_child_max_rss_bytes",
                4_194_304.0,
                {
                    "program": "cat",
                    "project": "resource-metrics",
                    "resource_usage_mode": "wait4_child",
                },
            ),
            (
                "cuprum_child_user_cpu_seconds",
                0.375,
                {
                    "program": "cat",
                    "project": "resource-metrics",
                    "resource_usage_mode": "wait4_child",
                },
            ),
            (
                "cuprum_child_system_cpu_seconds",
                0.125,
                {
                    "program": "cat",
                    "project": "resource-metrics",
                    "resource_usage_mode": "wait4_child",
                },
            ),
        ], (
            "an attributable measurement must emit every resource metric, each "
            "labelled with the mode, and must not add that mode to the duration "
            "histogram it shares the event with"
        )

    def test_cpu_only_fallback_omits_rss_and_names_its_mode(self) -> None:
        """The aggregate delta reports CPU without inventing an RSS figure."""
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        hook(
            self._exit_event(
                user_cpu_seconds=0.5,
                system_cpu_seconds=0.25,
                resource_usage_mode="aggregate_cpu_delta",
            )
        )

        observed = [name for name, _, _ in recorder.calls]
        assert "cuprum_child_max_rss_bytes" not in observed, (
            "an aggregate high-water mark is not attributable, so no RSS "
            f"histogram may be observed, got {observed!r}"
        )
        modes = {
            labels["resource_usage_mode"]
            for name, _, labels in recorder.calls
            if name.startswith("cuprum_child_") or name.endswith("measurements_total")
        }
        assert modes == {"aggregate_cpu_delta"}, (
            "every resource metric must name the fallback as its source"
        )

    def test_unavailable_measurement_counts_without_observing_values(self) -> None:
        """A platform that measures nothing is still counted as such.

        The counter is the whole point of the ``unavailable`` mode: a consumer
        seeing no resource histogram cannot otherwise tell a platform that
        cannot measure from a deployment where the metrics went missing.
        """
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        hook(self._exit_event(resource_usage_mode="unavailable"))

        assert recorder.calls == [
            (
                "cuprum_duration_seconds",
                0.25,
                {
                    "program": "cat",
                    "project": "unknown",
                },
            ),
            (
                "cuprum_resource_usage_measurements_total",
                1.0,
                {
                    "program": "cat",
                    "project": "unknown",
                    "resource_usage_mode": "unavailable",
                },
            ),
        ], (
            "an unmeasured platform must count its terminal event without "
            "observing any resource value"
        )

    def test_events_without_a_mode_emit_no_resource_metric(self) -> None:
        """A phase that attempted no measurement contributes no resource metric.

        This is what keeps the counter's meaning narrow: it counts terminal
        events that recorded a mode, so an unset mode — every non-terminal
        phase, and any event built before a measurement was attempted — must
        stay out of the series rather than inflating it as a zero-value entry.
        """
        recorder = _LabelRecordingCollector()
        hook = MetricsHook(recorder)

        hook(self._exit_event())

        observed = [name for name, _, _ in recorder.calls]
        assert observed == ["cuprum_duration_seconds"], (
            "only the duration histogram may be observed for an exit event "
            f"that recorded no resource mode, got {observed!r}"
        )

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("wait4_child", "wait4_child"),
            ("aggregate_cpu_delta", "aggregate_cpu_delta"),
            ("unavailable", "unavailable"),
        ],
    )
    def test_mode_label_is_the_recorded_mode(self, mode: str, expected: str) -> None:
        """Every accepted mode reaches the label unchanged.

        The label is what makes the resource series separable per source, so it
        must carry the recorded value rather than a normalized or reduced one.
        """
        recorder = _LabelRecordingCollector(record_histograms=False)
        hook = MetricsHook(recorder)

        hook(self._exit_event(resource_usage_mode=mode))

        assert recorder.labels == [
            {
                "program": "cat",
                "project": "unknown",
                "resource_usage_mode": expected,
            }
        ], f"the {mode!r} mode must reach the label unchanged"
