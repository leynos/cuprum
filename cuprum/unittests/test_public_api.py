"""Unit tests for cuprum public exports."""

from __future__ import annotations

import dataclasses as dc

import pytest

import cuprum as c
from cuprum import (
    context,
    echo_events,
    pump_events,
    pump_observation,
    pump_span_events,
    pump_span_observation,
)
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
    assert c.RustPumpHandoffOutcome is pump_events.RustPumpHandoffOutcome, (
        "RustPumpHandoffOutcome must be exported from cuprum.pump_events"
    )
    assert c.PumpHookRegistration is pump_observation.PumpHookRegistration, (
        "PumpHookRegistration must be exported from cuprum.pump_observation"
    )
    assert c.observe_pump is pump_observation.observe_pump, (
        "observe_pump must be exported from cuprum.pump_observation"
    )
    assert c.PumpHopOutcome is pump_span_events.PumpHopOutcome, (
        "PumpHopOutcome must be exported from cuprum.pump_span_events"
    )
    assert c.PumpHopSpanRegistration is pump_span_observation.PumpHopSpanRegistration, (
        "PumpHopSpanRegistration must come from cuprum.pump_span_observation"
    )
    assert c.observe_pump_span is pump_span_observation.observe_pump_span, (
        "observe_pump_span must come from cuprum.pump_span_observation"
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


def test_command_result_exposes_execution_measurements() -> None:
    """``CommandResult`` keeps exit semantics while exposing measurements."""
    fields = {field.name for field in dc.fields(c.CommandResult)}

    assert {
        "started_at",
        "duration",
        "max_rss_bytes",
        "user_cpu_seconds",
        "system_cpu_seconds",
    } <= fields
    legacy_result = c.CommandResult(c.ECHO, (), 0, 1, "", "")
    assert legacy_result.started_at == pytest.approx(0.0)
    assert legacy_result.duration == pytest.approx(0.0)
    assert legacy_result.ok is True
    assert (
        c.CommandResult(c.ECHO, (), 0, 1, "", "", started_at=0.0, duration=0.0).ok
        is True
    ), "supplying the measurements by keyword must still report success"
    assert (
        c.CommandResult(c.ECHO, (), 1, 1, "", "", started_at=0.0, duration=0.0).ok
        is False
    ), "supplying the measurements by keyword must not mask a failing exit code"


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


def test_relay_fallback_is_exported_from_its_definition_site() -> None:
    """The package-root RelayFallback is the echo_events definition."""
    assert c.RelayFallback is echo_events.RelayFallback, (
        "RelayFallback must be exported from cuprum.echo_events"
    )


def test_command_result_keeps_relay_fallbacks_as_its_trailing_slot() -> None:
    """``relay_fallbacks`` must stay the seventh positional CommandResult field.

    ``CommandResult`` is a public, non-``kw_only`` dataclass whose first seven
    fields are positional. Keeping ``relay_fallbacks`` seventh preserves the
    positional contract main established; the later measurement fields are
    keyword-only, so they cannot take a positional slot ahead of it. Inserting a
    positional field earlier silently rebinds a seven-argument construction --
    the relay tuple lands on the new field and ``relay_fallbacks`` falls back to
    ``()`` -- with no runtime error, because Python does not check argument
    types. This test pins that ordering and the keyword-only measurements.
    """
    fields = [f.name for f in dc.fields(c.CommandResult)]
    # ``kw_only`` moves a field out of the generated ``__init__``'s positional
    # order, not out of ``dc.fields()``, so the call contract is read off the
    # positional projection rather than the declaration index.
    positional = [f.name for f in dc.fields(c.CommandResult) if not f.kw_only]
    assert positional[6] == "relay_fallbacks", (
        "relay_fallbacks must be the seventh positional slot so existing "
        "seven-argument callers keep binding it; the measurement fields are "
        f"keyword-only and must never take a slot, got positional={positional}"
    )
    assert positional[-1] == "relay_fallbacks", (
        "relay_fallbacks must be the last positional field, so the generated "
        f"signature keeps it trailing, got positional={positional}"
    )
    assert fields[-1] == "relay_fallbacks", (
        "relay_fallbacks must stay last so existing positional callers keep "
        f"binding stdout and stderr, got {fields}"
    )
    result = c.CommandResult(
        c.Program(c.ECHO),  # program
        (),  # argv
        0,  # exit_code
        4242,  # pid
        "out",  # stdout
        "err",  # stderr
    )
    assert result.stdout == "out", (
        f"stdout must bind positionally, got {result.stdout!r}"
    )
    assert result.stderr == "err", (
        f"stderr must bind positionally, got {result.stderr!r}"
    )
    assert not result.relay_fallbacks, (
        f"the defaulted diagnostics must be empty, got {result.relay_fallbacks!r}"
    )

    sentinel = c.RelayFallback(
        stream=c.EchoStream.STDOUT,
        error_category=c.EchoErrorCategory.UNICODE_ENCODE,
    )
    relayed = c.CommandResult(c.ECHO, (), 0, 1, "out", "err", (sentinel,))
    assert relayed.relay_fallbacks == (sentinel,), (
        "a seventh positional argument must bind relay_fallbacks, got "
        f"{relayed.relay_fallbacks!r}"
    )
    assert relayed.started_at == pytest.approx(0.0), (
        "the measurements must not be reachable positionally, got "
        f"started_at={relayed.started_at!r}"
    )

    # The measurements are keyword-only, so they can never take an eighth
    # positional slot and silently absorb a call with too many arguments. The
    # arity is asserted at runtime on purpose: if a later change drops
    # ``kw_only``, the eighth argument would bind ``started_at`` with no error,
    # and this is the assertion that catches it. The call is unpacked and its
    # static rejection suppressed for the same reason -- a type checker refuses
    # the call before it can be made, which is exactly the failure under test.
    over_long: tuple[object, ...] = (
        c.ECHO,
        (),
        0,
        1,
        "out",
        "err",
        (sentinel,),
        0.0,
    )
    with pytest.raises(TypeError):
        c.CommandResult(*over_long)  # ty: ignore[too-many-positional-arguments] - the over-arity call is the assertion


def test_command_result_type_hints_resolve_at_runtime() -> None:
    """Public annotations on CommandResult resolve via typing.get_type_hints."""
    import typing as typ

    hints = typ.get_type_hints(c.CommandResult)
    assert hints["relay_fallbacks"] == tuple[c.RelayFallback, ...], (
        f"the annotation must resolve to the public record, got "
        f"{hints['relay_fallbacks']!r}"
    )
    assert hints["stdout"] == str | None


@pytest.mark.parametrize(
    ("qualname", "expected_return"),
    [
        ("SafeCmd.run", "CommandResult"),
        ("SafeCmd.run_sync", "CommandResult"),
        ("Pipeline.run", "PipelineResult"),
        ("Pipeline.run_sync", "PipelineResult"),
    ],
)
def test_execution_method_type_hints_resolve_at_runtime(
    qualname: str,
    expected_return: str,
) -> None:
    """Run methods resolve their result annotations via typing.get_type_hints."""
    import typing as typ

    from cuprum import sh

    owner_name, method_name = qualname.split(".")
    method = getattr(getattr(sh, owner_name), method_name)
    hints = typ.get_type_hints(method)
    assert hints["return"] is getattr(sh, expected_return), (
        f"{qualname} must resolve its return annotation to "
        f"cuprum.sh.{expected_return}, got {hints['return']!r}"
    )
    assert hints["context"] == sh.ExecutionContext | None


def test_safe_cmd_and_make_type_hints_resolve_at_runtime() -> None:
    """SafeCmd fields, pipeline composition, and make resolve their hints."""
    import typing as typ

    from cuprum import sh

    assert typ.get_type_hints(sh.SafeCmd)["project"] is c.ProjectSettings
    assert typ.get_type_hints(sh.Pipeline.concat)["return"] is sh.Pipeline
    make_hints = typ.get_type_hints(sh.make)
    assert make_hints["program"] is c.Program, (
        f"make must resolve its Program annotation, got {make_hints['program']!r}"
    )


def test_relay_fallback_is_frozen_with_bounded_fields() -> None:
    """RelayFallback is immutable and carries only closed-set vocabulary."""
    from cuprum.echo_events import EchoErrorCategory, EchoStream

    fallback = c.RelayFallback(
        stream=EchoStream.STDOUT,
        error_category=EchoErrorCategory.UNICODE_ENCODE,
    )
    fields = [f.name for f in dc.fields(c.RelayFallback)]
    assert fields == ["stream", "error_category"], (
        f"the record must stay bounded to the echo vocabulary, got {fields}"
    )
    with pytest.raises(dc.FrozenInstanceError):
        fallback.stream = EchoStream.STDERR  # type: ignore[misc]  # ty: ignore[invalid-assignment]
