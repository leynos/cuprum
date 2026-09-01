"""Tracing contract tests for native-pump cleanup events."""

from __future__ import annotations

from pathlib import Path

from cuprum.adapters.tracing_adapter import TracingHook
from cuprum.events import ExecEvent, new_exec_id
from cuprum.program import Program
from cuprum.pump_events import PumpEvent
from cuprum.unittests._adapter_test_support import (
    Traced,
    _cat_overrides,
    _make_exec_event,
    tracing_hook,
)
from tests.helpers import read_users_guide

__all__ = ["tracing_hook"]

DOCUMENTED_SPAN_ATTRIBUTES: frozenset[str] = frozenset({
    "cuprum.program",
    "cuprum.argv",
    "cuprum.pid",
    "cuprum.cwd",
    "cuprum.exit_code",
    "cuprum.duration_s",
    "cuprum.project",
    "cuprum.pipeline_stage_index",
    "cuprum.pipeline_stages",
})


class TestNativePumpCleanupTracing:
    """The pump channel records cleanup facts on their matching execution span."""

    def test_cleanup_events_attach_only_to_the_source_stage_span(
        self,
        tracing_hook: Traced,
    ) -> None:
        """A source token selects its open stage span without ending it."""
        tracer, hook = tracing_hook
        source_exec_id, downstream_exec_id = new_exec_id(), new_exec_id()
        hook(
            _make_exec_event(
                phase="start",
                overrides={
                    **_cat_overrides(source_exec_id),
                    "tags": {"pipeline_stage_index": 0, "pipeline_stages": 2},
                },
            )
        )
        source_span = tracer.spans[0]
        hook(
            _make_exec_event(
                phase="start",
                overrides={
                    **_cat_overrides(downstream_exec_id),
                    "tags": {"pipeline_stage_index": 1, "pipeline_stages": 2},
                },
            )
        )
        downstream_span = tracer.spans[1]

        hook.record_pump_event(
            PumpEvent(phase="cleanup_started", exec_id=source_exec_id)
        )
        hook.record_pump_event(
            PumpEvent(
                phase="cleanup_completed",
                duration_s=2.5,
                exec_id=source_exec_id,
            )
        )

        assert source_span.events == [
            (
                "cuprum.native_pump_cleanup_started",
                {"operation": "native_pump_cleanup", "outcome": "started"},
            ),
            (
                "cuprum.native_pump_cleanup_completed",
                {
                    "operation": "native_pump_cleanup",
                    "outcome": "completed",
                    "duration_s": 2.5,
                },
            ),
        ], f"cleanup events must attach to the source stage, found {source_span.events}"
        assert downstream_span.events == [], (
            "cleanup events must not attach to the downstream stage span"
        )
        assert source_span.ended is False, (
            "cleanup tracing must not end the source span"
        )
        assert source_span.status_ok is None, (
            "cleanup tracing must not mark the source span's status"
        )

    def test_cleanup_event_without_an_open_span_is_dropped(
        self,
        tracing_hook: Traced,
    ) -> None:
        """A missing active execution span is a safe no-op."""
        tracer, hook = tracing_hook

        hook.record_pump_event(
            PumpEvent(phase="cleanup_started", exec_id=new_exec_id())
        )

        assert tracer.spans == [], (
            "a cleanup event without a matching open span must not create one"
        )

    def test_users_guide_names_the_cleanup_trace_contract(self) -> None:
        """The public guide retains the stable cleanup tracing names."""
        guide = read_users_guide()
        expected = {
            "cuprum.native_pump_cleanup_started",
            "cuprum.native_pump_cleanup_completed",
            "observe_pump(hook.record_pump_event)",
            'operation="native_pump_cleanup"',
        }
        missing = sorted(item for item in expected if item not in guide)
        assert not missing, f"users' guide omits cleanup tracing contract: {missing}"

    def test_emitted_attributes_match_documented_contract(self) -> None:
        """The initial and exit attributes equal the documented contract."""
        start_event = ExecEvent(
            phase="start",
            program=Program("cat"),
            argv=("cat",),
            cwd=Path("/srv/work"),
            env=None,
            pid=4321,
            timestamp=0.0,
            line=None,
            exit_code=None,
            duration_s=None,
            tags={
                "project": "doc-lockstep",
                "pipeline_stage_index": 0,
                "pipeline_stages": 2,
            },
        )
        emitted = set(TracingHook._build_attributes(start_event))
        emitted |= {"cuprum.exit_code", "cuprum.duration_s"}

        contract = set(DOCUMENTED_SPAN_ATTRIBUTES)
        assert emitted == contract, (
            "emitted attributes must match the documented contract; "
            f"emitted-only={emitted - contract}, contract-only={contract - emitted}"
        )

    def test_docstring_documents_each_contract_attribute(self) -> None:
        """The ``TracingHook`` docstring names every contract attribute."""
        doc = TracingHook.__doc__ or ""
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"``{attr}``" not in doc
        )
        assert not missing, f"TracingHook docstring omits attributes: {missing}"

    def test_users_guide_lists_every_tracing_attribute(self) -> None:
        """The users' guide lists every documented tracing attribute."""
        guide = read_users_guide()
        missing = sorted(
            attr for attr in DOCUMENTED_SPAN_ATTRIBUTES if f"`{attr}`" not in guide
        )
        assert not missing, f"users' guide omits tracing attributes: {missing}"

    def test_users_guide_names_record_output_option(self) -> None:
        """The users' guide documents ``record_output``, not the obsolete name."""
        guide = read_users_guide()
        assert "record_output" in guide, (
            "users' guide must name the record_output option"
        )
        assert "record_io" not in guide, (
            "users' guide must not use the obsolete record_io option name"
        )
