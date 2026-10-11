"""Property tests for common logging, tracing, and metrics projections."""

from __future__ import annotations

import typing as typ
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.adapters._support import _event_common_fields
from cuprum.adapters.logging_adapter import _build_extra
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.context import EnvMode
from cuprum.events import (
    ExecEvent,
    ExecPhase,
    ResourceUsageMode,
    TerminalOutcome,
)
from cuprum.program import Program

_OPTIONAL_FIELDS = (
    "pid",
    "cwd",
    "exit_code",
    "duration_s",
    "stage_index",
    "stage_count",
    "line",
    "max_rss_bytes",
    "user_cpu_seconds",
    "system_cpu_seconds",
    "resource_usage_mode",
    "env_mode",
    "terminal_outcome",
    "resolved_path",
)
_PHASES = typ.get_args(ExecPhase.__value__)


@st.composite
def _events(draw: st.DrawFn) -> ExecEvent:
    """Generate events with every optional field independently present/absent."""
    return ExecEvent(
        phase=draw(st.sampled_from(_PHASES)),
        program=Program("echo"),
        argv=("echo", "hello"),
        cwd=draw(st.none() | st.just(Path("/srv/work"))),
        env=None,
        pid=draw(st.none() | st.integers(min_value=1, max_value=99_999)),
        timestamp=0.0,
        line=draw(st.none() | st.just("a line")),
        exit_code=draw(st.none() | st.integers(min_value=0, max_value=255)),
        duration_s=draw(st.none() | st.just(0.125)),
        tags=draw(
            st.dictionaries(
                st.sampled_from(
                    ("project", "pipeline_stage_index", "pipeline_stages"),
                ),
                st.none()
                | st.text(max_size=20)
                | st.integers(min_value=0, max_value=5),
                max_size=3,
            ),
        ),
        project=draw(st.none() | st.text(max_size=20)),
        stage_index=draw(st.none() | st.integers(min_value=0, max_value=7)),
        stage_count=draw(st.none() | st.integers(min_value=1, max_value=8)),
        max_rss_bytes=draw(st.none() | st.integers(min_value=0, max_value=2**32)),
        user_cpu_seconds=draw(st.none() | st.floats(min_value=0.0, max_value=60.0)),
        system_cpu_seconds=draw(st.none() | st.floats(min_value=0.0, max_value=60.0)),
        resource_usage_mode=draw(st.none() | st.sampled_from(ResourceUsageMode)),
        env_mode=draw(st.none() | st.sampled_from(EnvMode)),
        terminal_outcome=draw(st.none() | st.sampled_from(TerminalOutcome)),
        # An absolute path when present, so the projection carries a string
        # rather than an object needing rendering. ``resolved_path`` is the
        # one verbatim field that names the executed binary, so it must travel
        # unchanged and must not be stringified the way ``cwd`` is.
        resolved_path=draw(st.none() | st.just("/opt/tools/echo")),
    )


def _expected_projection(event: ExecEvent) -> dict[str, object]:
    """Mirror the projection contract: ``None`` omitted, ``cwd`` stringified."""
    expected: dict[str, object] = {
        "program": str(event.program),
        "argv": event.argv,
    }
    present = (
        (field, getattr(event, field))
        for field in _OPTIONAL_FIELDS
        if getattr(event, field) is not None
    )
    for field, value in present:
        expected[field] = str(value) if field == "cwd" else value
    return expected


class TestAdapterProjectionProperties:
    """Hypothesis checks for common telemetry field projections."""

    @settings(
        deadline=None,
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(event=_events())
    def test_projection_includes_exactly_the_non_none_fields(
        self,
        event: ExecEvent,
    ) -> None:
        """Property: the canonical projection omits exactly the ``None`` fields.

        Parameters
        ----------
        event : ExecEvent
            Generated event with optional fields independently present or absent.
        """
        projected = dict(_event_common_fields(event, lambda field: field))

        assert projected == _expected_projection(event), (
            "projection must carry program, argv, and exactly the non-None "
            "optional fields (cwd stringified)"
        )

    @settings(
        deadline=None,
        max_examples=50,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @given(event=_events())
    def test_adapters_agree_on_common_keys_modulo_prefix(
        self,
        event: ExecEvent,
    ) -> None:
        """Property: the three adapters expose the same common key set.

        The logging extras (``cuprum_`` prefix) and tracing attributes
        (``cuprum.`` prefix) must carry the same canonical fields; the metrics
        labels are the deliberate low-cardinality subset (``program`` plus
        ``project``).

        Parameters
        ----------
        event : ExecEvent
            Generated event with optional fields independently present or absent.
        """
        canonical = {key for key, _ in _event_common_fields(event, lambda field: field)}
        self._assert_logging_projection(event, canonical)
        self._assert_tracing_projection(event, canonical)
        self._assert_metrics_labels(event)

    @staticmethod
    def _assert_logging_projection(event: ExecEvent, canonical: set[str]) -> None:
        """Assert the logging projection preserves its canonical fields."""
        extra = _build_extra(event)
        extra_keys = {
            key.removeprefix("cuprum_") for key in extra if key != "cuprum_phase"
        }
        assert extra_keys == TestAdapterProjectionProperties._expected_logging_fields(
            event, canonical
        ), (
            "logging extras must expose exactly the canonical common fields after "
            "removing their backend prefix"
        )
        TestAdapterProjectionProperties._assert_phase_specific_logging_rules(
            event, extra
        )

    @staticmethod
    def _expected_logging_fields(event: ExecEvent, canonical: set[str]) -> set[str]:
        """Return the structured-log fields the event phase may expose."""
        if event.phase == "pipeline_fail_fast":
            return (canonical - {"argv"}) | {"exec_id"}
        if event.phase != "capture_eof_grace_expired":
            return canonical

        trusted_fields = (
            "pid",
            "project",
            "exec_id",
            "operation",
            "eof_grace_s",
            "pending_readers",
            "env_mode",
        )
        return {"program"} | {
            field for field in trusted_fields if getattr(event, field) is not None
        }

    @staticmethod
    def _assert_phase_specific_logging_rules(
        event: ExecEvent,
        extra: dict[str, object],
    ) -> None:
        """Assert phase-specific privacy and correlation rules for log extras."""
        if event.phase == "pipeline_fail_fast":
            assert extra["cuprum_exec_id"] == event.exec_id, (
                "fail-fast extras must preserve the execution correlation token"
            )
            assert "cuprum_argv" not in extra, (
                "fail-fast extras must omit the raw argument vector"
            )
        elif event.phase == "capture_eof_grace_expired":
            assert "cuprum_argv" not in extra, (
                "grace-expiry extras must omit the raw argument vector"
            )
            if event.exec_id is not None:
                assert extra["cuprum_exec_id"] == event.exec_id, (
                    "grace-expiry extras must preserve execution correlation"
                )
        else:
            assert extra["cuprum_argv"] == event.argv, (
                "logging extras must preserve argv as a tuple"
            )
        assert "cuprum_tags" not in extra, (
            "logging extras must omit arbitrary event tags"
        )

    @staticmethod
    def _assert_tracing_projection(event: ExecEvent, canonical: set[str]) -> None:
        """Assert the tracing projection preserves its canonical fields."""
        attr_keys = {
            key.removeprefix("cuprum.")
            for key in TracingHook._build_attributes(event)
            if key
            not in {
                "cuprum.project",
                "cuprum.pipeline_stage_index",
                "cuprum.pipeline_stages",
            }
        }
        assert attr_keys == canonical, (
            "tracing attributes must expose exactly the canonical common fields "
            "after removing their backend prefix"
        )
        assert TracingHook._build_attributes(event)["cuprum.argv"] == list(
            event.argv
        ), "tracing attributes must render argv as a list"

    @staticmethod
    def _assert_metrics_labels(event: ExecEvent) -> None:
        """Assert metrics retain only their low-cardinality labels."""
        labels = MetricsHook._extract_labels(event)
        assert set(labels) == {"program", "project"}, (
            "metrics labels must stay limited to the low-cardinality program and "
            "project fields"
        )
        assert labels["program"] == str(event.program), (
            "metrics labels must stringify the event program when it is present"
        )
        project = (
            event.project
            if event.phase == "pipeline_fail_fast"
            else event.tags.get("project")
        )
        expected_project = str(project) if project is not None else ""
        assert labels["project"] == (expected_project or "unknown"), (
            "metrics labels must stringify a non-empty project tag and fall back "
            "to 'unknown' when the tag is absent, None, or empty"
        )
