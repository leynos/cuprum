"""Unit tests for cuprum public exports."""

from __future__ import annotations

import dataclasses as dc

import cuprum as c
from cuprum import context, pump_events, pump_observation
from cuprum.events import ExecHook, new_exec_id


def test_public_exports_are_available() -> None:
    """Top-level cuprum exports the catalogue and pump observation symbols."""
    assert c.DEFAULT_CATALOGUE is not None, "DEFAULT_CATALOGUE must be exported"
    assert c.DEFAULT_PROJECTS, "DEFAULT_PROJECTS must not be empty"
    assert c.CORE_OPS_PROJECT == "core-ops", "CORE_OPS_PROJECT value mismatch"
    assert c.DOCUMENTATION_PROJECT == "docs", "DOCUMENTATION_PROJECT value mismatch"
    assert c.Program("echo") == c.ECHO, "ECHO must round-trip via Program"
    assert c.Program("git") == c.GIT, "GIT must round-trip via Program"
    assert c.Program("ls") == c.LS, "LS must round-trip via Program"
    assert c.Program("rsync") == c.RSYNC, "RSYNC must round-trip via Program"
    assert c.Program("tar") == c.TAR, "TAR must round-trip via Program"
    assert c.Program("mdbook") == c.DOC_TOOL, "DOC_TOOL must round-trip via Program"
    assert c.builders is not None, "builders package should be exported"
    assert c.ProgramCatalogue is not None, "ProgramCatalogue must be exported"
    assert c.ProgramEntry is not None, "ProgramEntry must be exported"
    assert c.ProjectSettings is not None, "ProjectSettings must be exported"
    assert c.UnknownProgramError is not None, "UnknownProgramError must be exported"
    assert c.Pipeline is not None, "Pipeline must be exported"
    assert c.PipelineResult is not None, "PipelineResult must be exported"
    assert callable(c.is_rust_available), "is_rust_available must be exported"
    # The pump observation channel is documented as a top-level surface in the
    # changelog and ADR-008. Pinned by identity against its defining module so
    # dropping a re-export, or re-pointing one at a different definition, fails
    # here rather than in a caller's import.
    assert c.PumpEvent is pump_events.PumpEvent, (
        "PumpEvent must be exported from cuprum.pump_events"
    )
    assert c.PumpHook is pump_events.PumpHook, (
        "PumpHook must be exported from cuprum.pump_events"
    )
    assert c.RustPumpDeclineReason is pump_events.RustPumpDeclineReason, (
        "RustPumpDeclineReason must be exported from cuprum.pump_events"
    )
    assert c.PumpHookRegistration is pump_observation.PumpHookRegistration, (
        "PumpHookRegistration must be exported from cuprum.pump_observation"
    )
    assert c.observe_pump is pump_observation.observe_pump, (
        "observe_pump must be exported from cuprum.pump_observation"
    )


def test_exec_hook_uses_events_as_its_definition_site() -> None:
    """ExecHook remains top-level but is no longer re-exported by context."""
    assert c.ExecHook is ExecHook, "top-level ExecHook must come from events"
    assert not hasattr(context, "ExecHook"), "context must not re-export ExecHook"
    assert "ExecHook" not in context.__all__, "context.__all__ must omit ExecHook"


def test_public_catalogue_behaviour_via_reexports() -> None:
    """Catalogue lookups work through the re-exported API surface."""
    entry = c.DEFAULT_CATALOGUE.lookup(c.ECHO)
    assert entry.program == c.Program("echo"), "Lookup must return typed Program"
    assert entry.project_name == c.CORE_OPS_PROJECT, "Project name mismatch"
    assert c.DEFAULT_CATALOGUE.is_allowed("ls"), "Curated program ls must be allowed"
    assert not c.DEFAULT_CATALOGUE.is_allowed("definitely-not-allowed"), (
        "Unknown program should not be allowlisted"
    )


def test_exec_id_keeps_its_positional_slot() -> None:
    """``exec_id`` must stay the first optional field after ``error_type``.

    ``ExecEvent`` is a public, non-``kw_only`` dataclass, so callers may build
    one positionally. Inserting a new optional field ahead of ``exec_id``
    silently rebinds such a call: the correlation token lands on the new field
    and ``exec_id`` falls back to ``None``, at which point consumers like
    ``TracingHook`` treat the event as uncorrelatable and drop it. New optional
    fields therefore go after ``exec_id``, and this pins that ordering.
    """
    fields = [f.name for f in dc.fields(c.ExecEvent)]
    assert fields.index("exec_id") == fields.index("error_type") + 1, (
        "exec_id must directly follow error_type so existing positional callers "
        f"keep binding it, got {fields}"
    )

    exec_id = new_exec_id()
    event = c.ExecEvent(
        "start",  # phase
        c.ECHO,  # program
        ("echo",),  # argv
        None,  # cwd
        None,  # env
        4321,  # pid
        0.0,  # timestamp
        None,  # line
        None,  # exit_code
        None,  # duration_s
        {},  # tags
        None,  # note
        None,  # byte_count
        None,  # operation
        None,  # error_type
        exec_id,  # exec_id
    )
    assert event.exec_id == exec_id, (
        f"positional construction must still bind exec_id, got {event.exec_id!r} "
        f"with timeout_s={event.timeout_s!r}"
    )
    assert event.timeout_s is None, (
        f"the correlation token must not land on timeout_s, got {event.timeout_s!r}"
    )
