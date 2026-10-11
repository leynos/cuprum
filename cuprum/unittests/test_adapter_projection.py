"""Snapshot tests for the canonical adapter event projection.

The tracing, metrics, and logging adapters previously each re-implemented the
"include the field only when not ``None``" projection of an ``ExecEvent`` and
disagreed on the key prefix. ``cuprum.adapters._support._event_common_fields``
is now the single source of truth (#114). Property tests for the common field
set live in ``test_adapter_projection_properties.py``; this module locks the
projected dictionaries for each phase with syrupy snapshots after redacting
volatile fields.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from pathlib import Path

import pytest

from cuprum.adapters.logging_adapter import _build_extra
from cuprum.adapters.metrics_adapter import MetricsHook
from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.adapters.tracing_memory import InMemoryTracer
from cuprum.context import EnvMode
from cuprum.events import (
    ExecEvent,
    ExecPhase,
    ResourceUsageMode,
    StdioFailureCategory,
    TerminalOutcome,
    new_exec_id,
)
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

_REDACTED_FIELDS = frozenset({"pid", "duration_s", "cwd"})
_PHASES = typ.get_args(ExecPhase.__value__)
# Ancillary diagnostic phases and the ``operation`` each reports. These are the
# phases whose structured fields travel as a span event rather than as span
# attributes.
_ANCILLARY_PHASES = {
    "stdin_error": "write",
    "stdio_error": "produce",
    "timeout": "wait",
    "teardown_error": "drain",
    "capture_eof_grace_expired": "drain",
}


class TestAdapterProjection:
    """Tests for the canonical telemetry adapter projection."""

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
    def _representative_event(phase: str) -> ExecEvent:
        """Build a deterministic, fully populated event for *phase*."""
        is_exit = phase == "exit"
        is_output = phase in {"stdout", "stderr"}
        is_timeout = phase == "timeout"
        is_grace_expiry = phase == "capture_eof_grace_expired"
        is_stdio_error = phase == "stdio_error"
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
            error_category=(StdioFailureCategory.PRODUCER if is_stdio_error else None),
            note="consumer drain failed: ValueError"
            if phase == "teardown_error"
            else None,
            timeout_s=1.5 if is_timeout else None,
            timeout_mode="elapsed_deadline" if is_timeout else None,
            stage_index=0 if is_fail_fast else None,
            stage_count=2 if is_fail_fast else None,
            eof_grace_s=0.25 if is_grace_expiry else None,
            pending_readers=1 if is_grace_expiry else None,
            # Fixed, not volatile: the snapshot may pin the exact figures the
            # attributable path publishes, unlike pid and duration.
            max_rss_bytes=4_194_304 if is_exit else None,
            user_cpu_seconds=0.375 if is_exit else None,
            system_cpu_seconds=0.125 if is_exit else None,
            resource_usage_mode=ResourceUsageMode.WAIT4_CHILD if is_exit else None,
            terminal_outcome=(
                TerminalOutcome.EXIT_ZERO if phase == "settled" else None
            ),
            # Present on every phase, unlike the resource mode: the policy is
            # known before the child is spawned and describes the whole
            # execution rather than one measurement.
            env_mode=EnvMode.REPLACE,
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
        logging_extra = _build_extra(event)
        tracing_attributes = TracingHook._build_attributes(event)
        projections = {
            "logging_extra": self._redact(logging_extra),
            "tracing_attributes": self._redact(tracing_attributes),
            "metrics_labels": self._redact(dict(MetricsHook._extract_labels(event))),
            "tracing_span_event": self._span_event_projection(event),
        }
        if phase == "settled":
            assert logging_extra.get("cuprum_terminal_outcome") == "exit_zero", (
                "settled logging snapshots must expose the terminal category"
            )
            assert tracing_attributes.get("cuprum.terminal_outcome") == "exit_zero", (
                "settled tracing snapshots must expose the terminal category"
            )
        if phase == "stdio_error":
            # The boundary category is the whole point of the phase, so it is
            # asserted semantically as well as snapshotted: a regression that
            # dropped it would leave the record naming "a write failed" without
            # saying which of the boundaries sharing an exception class did.
            assert logging_extra.get("cuprum_error_category") == "producer", (
                "stdio-failure records must name the failing boundary"
            )
            assert (
                projections["tracing_span_event"].get("error_category") == "producer"
            ), "span events must name the failing boundary"
            assert set(projections["metrics_labels"]) == {"program", "project"}, (
                "the boundary category must not become a common metric label"
            )
        assert projections == snapshot, (
            "per-phase adapter projections must match the redacted wire-contract "
            "snapshot"
        )
