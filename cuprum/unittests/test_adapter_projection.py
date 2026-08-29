"""Property and snapshot tests for the canonical adapter event projection.

The tracing, metrics, and logging adapters previously each re-implemented the
"include the field only when not ``None``" projection of an ``ExecEvent`` and
disagreed on the key prefix. ``cuprum.adapters._support._event_common_fields``
is now the single source of truth (#114); these tests pin its contract:

- the projection yields exactly the non-``None`` optional fields;
- all three adapters agree on the common key set, modulo the parameterized
  prefix; and
- the projected dictionaries for a representative event in each phase are
  locked with syrupy snapshots (volatile fields redacted).
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum.adapters._support import _event_common_fields
from cuprum.adapters.logging_adapter import _build_extra
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.events import ExecEvent, ExecPhase, new_exec_id
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_OPTIONAL_FIELDS = (
    "pid",
    "cwd",
    "exit_code",
    "duration_s",
    "stage_index",
    "stage_count",
    "line",
)
_PHASES = typ.get_args(ExecPhase.__value__)
_REDACTED_FIELDS = frozenset({"pid", "duration_s", "cwd"})
# Ancillary diagnostic phases and the ``operation`` each reports. These are the
# phases whose structured fields travel as a span event rather than as span
# attributes.
_ANCILLARY_PHASES = {
    "stdin_error": "write",
    "timeout": "wait",
    "teardown_error": "drain",
    "capture_eof_grace_expired": "drain",
}


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


class TestAdapterProjection:
    """Tests for the canonical telemetry adapter projection."""

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
        assert extra_keys == TestAdapterProjection._expected_logging_fields(
            event, canonical
        ), (
            "logging extras must expose exactly the canonical common fields after "
            "removing their backend prefix"
        )
        TestAdapterProjection._assert_phase_specific_logging_rules(event, extra)

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

    def test_logging_extras_exclude_untrusted_tags(self) -> None:
        """Structured records never retain caller-controlled tag values."""
        event = dc.replace(
            self._representative_event("start"),
            tags={"token": "secret", "email": "person@example.test"},
        )

        extra = _build_extra(event)

        assert "cuprum_tags" not in extra, "structured logs must not expose tags"
        assert "secret" not in extra.values(), "structured logs must not expose tokens"
        assert "person@example.test" not in extra.values(), (
            "structured logs must not expose personal data"
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

    @staticmethod
    def _representative_event(phase: str) -> ExecEvent:
        """Build a deterministic, fully populated event for *phase*."""
        is_exit = phase == "exit"
        is_output = phase in {"stdout", "stderr"}
        is_timeout = phase == "timeout"
        is_grace_expiry = phase == "capture_eof_grace_expired"
        is_fail_fast = phase == "pipeline_fail_fast"
        ancillary = phase in _ANCILLARY_PHASES
        return ExecEvent(
            phase=typ.cast("ExecPhase", phase),
            program=Program("echo"),
            argv=("echo", "hello"),
            cwd=Path("/srv/work"),
            env=None,
            pid=None if phase in {"plan", "pipeline_fail_fast"} else 4321,
            timestamp=0.0,
            line="a line" if is_output else None,
            exit_code=0 if is_exit else 3 if is_fail_fast else None,
            duration_s=0.125 if is_exit or is_fail_fast else None,
            tags={
                "project": "proj",
                "pipeline_stage_index": 0,
                "pipeline_stages": 2,
            },
            project="proj",
            operation=_ANCILLARY_PHASES.get(phase),
            error_type="TimeoutError"
            if is_timeout
            else ("ValueError" if ancillary and not is_grace_expiry else None),
            note="consumer drain failed: ValueError"
            if phase == "teardown_error"
            else None,
            timeout_s=1.5 if is_timeout else None,
            timeout_mode="elapsed_deadline" if is_timeout else None,
            stage_index=0 if is_fail_fast else None,
            stage_count=2 if is_fail_fast else None,
            eof_grace_s=0.25 if is_grace_expiry else None,
            pending_readers=1 if is_grace_expiry else None,
        )

    @staticmethod
    def _redact(mapping: dict[str, object]) -> dict[str, object]:
        """Replace volatile fields (pid, duration, cwd) with stable tokens."""
        redacted: dict[str, object] = {}
        for key, value in mapping.items():
            field = key.removeprefix("cuprum.").removeprefix("cuprum_")
            if field in _REDACTED_FIELDS:
                redacted[key] = f"<{field}>"
            else:
                redacted[key] = value
        return redacted

    @staticmethod
    def _span_event_projection(event: ExecEvent) -> dict[str, object] | None:
        """Return the span event an ancillary phase records, or ``None``.

        ``_build_extra`` and ``_build_attributes`` both project through
        ``_event_common_fields``, which carries only the lifecycle fields — so
        neither can pin ``operation``/``error_type``/``timeout_s``/
        ``timeout_mode``. Tracing surfaces those through ``span.add_event``
        instead, and this is the projection that locks them: an adapter that
        dropped a field would change this snapshot.

        A span must be open for the ancillary event to attach to, so a ``start``
        sharing its ``exec_id`` is fed first.

        Returns
        -------
        dict[str, object] | None
            The span event's name and attributes, or ``None`` for a phase that
            records no ancillary event.
        """
        operation = _ANCILLARY_PHASES.get(event.phase)
        if operation is None:
            return None
        exec_id = new_exec_id()
        tracer = InMemoryTracer()
        hook = TracingHook(tracer)
        started = TestAdapterProjection._representative_event("start")
        hook(dc.replace(started, exec_id=exec_id))
        hook(dc.replace(event, exec_id=exec_id))
        name, attributes = tracer.spans[0].events[-1]
        return {"name": name, **attributes}

    @pytest.mark.parametrize("timeout_s", [0.0, -1.5])
    def test_non_positive_expiry_projects_both_timeout_fields(
        self, timeout_s: float
    ) -> None:
        """A non-positive deadline carries its own mode *and* its own timeout.

        The snapshots above fix the elapsed-deadline case, where ``timeout_s``
        is a truthy 1.5. This pins the other mode, whose configured timeout is
        ``0`` or negative: the projection includes a field when it is not
        ``None``, so a regression to a falsy test would silently drop
        ``timeout_s=0.0`` and leave a consumer unable to tell an immediate
        expiry's configured deadline from an unset one.
        """
        event = dc.replace(
            self._representative_event("timeout"),
            timeout_mode="non_positive_immediate",
            timeout_s=timeout_s,
        )
        attributes = self._span_event_projection(event)
        assert attributes is not None, "the timeout phase must project a span event"
        assert attributes.get("timeout_mode") == "non_positive_immediate", (
            "an immediate expiry must be distinguishable from an elapsed "
            f"deadline, got {attributes.get('timeout_mode')!r}"
        )
        assert attributes.get("timeout_s") == pytest.approx(timeout_s), (
            "the configured non-positive timeout must survive the projection, "
            f"got {attributes.get('timeout_s')!r}"
        )

    @pytest.mark.parametrize("phase", _PHASES)
    def test_projection_snapshots_lock_the_wire_contract(
        self,
        phase: str,
        snapshot: SnapshotAssertion,
    ) -> None:
        """Snapshot: the per-phase projected dictionaries are stable.

        Locks the multivariant output format across the three adapters for a
        representative event in each phase. Volatile fields (pid, duration, cwd)
        are redacted with stable tokens; the surrounding property tests assert
        their semantics.
        """
        event = self._representative_event(phase)
        projections = {
            "logging_extra": self._redact(_build_extra(event)),
            "tracing_attributes": self._redact(TracingHook._build_attributes(event)),
            "metrics_labels": self._redact(dict(MetricsHook._extract_labels(event))),
            "tracing_span_event": self._span_event_projection(event),
        }
        assert projections == snapshot, (
            "per-phase adapter projections must match the redacted wire-contract "
            "snapshot"
        )
