"""Execution tests for nested, per-call, and pipeline environment policies."""

from __future__ import annotations

import os
import typing as typ

import pytest

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.context import (
    CuprumContext,
    EnvMode,
    EnvRegistration,
    ScopeConfig,
    current_context,
    env,
    scoped,
)
from cuprum.program import Program
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _print_values(*names: str) -> str:
    """Return a program that prints requested environment values."""
    return (
        "import os;print('|'.join(os.environ.get(name, '<missing>') for name in "
        f"{names!r}))"
    )


def _bare_name_builder(name: str) -> cabc.Callable[[], SafeCmd]:
    """Return a builder for a program addressed by its bare name.

    Unlike :func:`python_builder`, which allowlists an absolute interpreter
    path, this allowlists a name with no directory component. Whether the child
    starts at all then depends on the rendered ``PATH``, which is what the
    replacement-path tests need to observe.

    Returns
    -------
    cabc.Callable[[], SafeCmd]
        A builder that produces commands for the bare-name program.
    """
    program = Program(name)
    catalogue = ProgramCatalogue.from_programs(
        program,
        name="bare-name-probes",
        documentation_locations=("docs/users-guide.md#environment-policy-modes",),
    )
    return sh.make(program, catalogue=catalogue)


def test_nested_replace_discards_outer_and_per_call_wins(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Nested replacement boundaries and per-call policy retain their precedence."""
    outer = "CUPRUM_TEST_NESTED_OUTER"
    inner = "CUPRUM_TEST_NESTED_INNER"
    call = "CUPRUM_TEST_NESTED_CALL"
    command = python_builder("-c", _print_values(outer, inner, call))

    with env({outer: "outer"}, mode=EnvMode.REPLACE):
        with env({inner: "inner"}):
            nested = command.run_sync()
        per_call = command.run_sync(
            context=ExecutionContext(
                env={call: "per-call"},
                env_mode=EnvMode.REPLACE,
            )
        )

    assert nested.stdout == "outer|inner|<missing>\n", (
        "an overlay inside replacement must retain the replacement boundary"
    )
    assert per_call.stdout == "<missing>|<missing>|per-call\n", (
        "a per-call replacement must discard every ambient environment layer"
    )


def test_replace_policy_reaches_every_pipeline_stage(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A pipeline uses the same replacement policy as a single command."""
    inherited = "CUPRUM_TEST_PIPELINE_INHERITED"
    supplied = "CUPRUM_TEST_PIPELINE_SUPPLIED"
    monkeypatch.setenv(inherited, "parent")
    parent_snapshot = dict(os.environ)
    # Both stages sample their own environment rather than the consumer acting
    # as a pass-through. ``_pipeline_spawn`` merges the policy per stage, so a
    # consumer that only forwarded stdin would leave a regression confined to
    # the second stage undetected: the producer's report would stay correct.
    stage_values = _print_values(inherited, supplied)
    producer = python_builder("-c", stage_values)
    consumer = python_builder(
        "-c", f"import sys;print(sys.stdin.read().strip());{stage_values}"
    )

    result = (producer | consumer).run_sync(
        context=ExecutionContext(
            env={supplied: "stage"},
            env_mode=EnvMode.REPLACE,
        )
    )

    assert result.stdout == "<missing>|stage\n<missing>|stage\n", (
        "every pipeline stage must receive the replacement environment"
    )
    assert dict(os.environ) == parent_snapshot, (
        "pipeline execution must not mutate os.environ"
    )


def test_inherit_mode_never_escapes_an_outer_replacement_boundary(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An inherit scope keeps the outer replacement boundary it was created in.

    ``INHERIT`` records that an outer scope already chose the policy, so an
    inner inherit scope must not resurrect the environment a replacement
    boundary discarded.

    The composition rule is what this pins, not the rendering: overlay and
    inherit render identically by design, so a regression that gave ``INHERIT``
    its own rendering branch is invisible here and is caught by
    ``test_inherit_renders_exactly_as_overlay`` instead. What a rendering test
    cannot see is a composition that treated ``INHERIT`` as a fresh boundary,
    and that is exactly what this asserts against.

    Both the scoped and the per-call route are exercised, because they resolve
    the mode through different entry points.
    """
    escaped = "CUPRUM_TEST_INHERIT_ESCAPED"
    own = "CUPRUM_TEST_INHERIT_OWN"
    monkeypatch.setenv(escaped, "parent-value")
    command = python_builder("-c", _print_values(escaped, own))

    with env({"CUPRUM_TEST_INHERIT_OUTER": "outer"}, mode=EnvMode.REPLACE):
        with env({own: "scoped"}, mode=EnvMode.INHERIT):
            scoped_run = command.run_sync()
        per_call = command.run_sync(
            context=ExecutionContext(env={own: "per-call"}, env_mode=EnvMode.INHERIT)
        )

    assert scoped_run.stdout == "<missing>|scoped\n", (
        "an inherit scope inside a replacement boundary must not read the live "
        "environment that boundary discarded"
    )
    assert per_call.stdout == "<missing>|per-call\n", (
        "a per-call inherit policy inside a replacement boundary must not read "
        "the live environment that boundary discarded"
    )


def test_scoped_replacement_reaches_the_subprocess_and_its_events(
    monkeypatch: pytest.MonkeyPatch,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A scoped replacement discards the outer overlay and reaches the child.

    The scoped route resolves its policy in ``CuprumContext.narrow``, which
    passes the parent mode and the config mode to ``_resolve_env_policy``. Both
    default to ``OVERLAY``, so every scoped test that leaves the mode unset
    stays green even when those two arguments are swapped, and the swap is
    invisible even here unless the outer layer carries an overlay of its own:
    with an empty parent overlay the swapped call still lands on ``REPLACE``
    and still composes an equally empty mapping. Nesting a replacement scope
    inside a populated outer one is therefore the arrangement that can see the
    difference — the outer value must reach neither the child nor the events.
    """
    outer_var = "CUPRUM_TEST_SCOPED_REPLACE_OUTER"
    inner_var = "CUPRUM_TEST_SCOPED_REPLACE_INNER"
    monkeypatch.setenv("CUPRUM_TEST_SCOPED_REPLACE_LIVE", "parent-value")
    parent_snapshot = dict(os.environ)
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(ev)

    command = python_builder(
        "-c", _print_values("CUPRUM_TEST_SCOPED_REPLACE_LIVE", outer_var, inner_var)
    )
    with (
        sh.observe(hook),
        scoped(ScopeConfig(env_overlay={outer_var: "outer"})),
        scoped(ScopeConfig(env_overlay={inner_var: "inner"}, env_mode=EnvMode.REPLACE)),
    ):
        result = command.run_sync()

    assert result.stdout == "<missing>|<missing>|inner\n", (
        "a scoped replacement must discard the live environment and every outer "
        "overlay, keeping only its own"
    )
    assert events, "the scoped run must emit observe events"
    assert all(ev.env_mode is EnvMode.REPLACE for ev in events), (
        f"a scoped replacement must report replace on every phase, got "
        f"{[ev.env_mode for ev in events]!r}"
    )
    assert dict(os.environ) == parent_snapshot, (
        "a scoped replacement must not mutate os.environ"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX bare-name PATH resolution")
def test_replacement_path_resolves_a_bare_program_name(tmp_path: Path) -> None:
    """A replacement ``PATH`` is what makes a bare program name resolvable.

    Every other replacement test in this module runs an absolute
    ``sys.executable``, which spawns under any policy and so proves nothing
    about the rendered ``PATH``. Here the program is a bare name, and the only
    thing that can find it is the ``PATH`` supplied by the replacement overlay.
    That is the arrangement the user guide describes, and it is the one that
    makes a missing ``PATH`` a spawn failure rather than a silent fallback to
    the live environment.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    probe = bindir / "cuprum-probe-bare-name"
    probe.write_text("#!/bin/sh\necho resolved-from-replacement-path\n")
    probe.chmod(0o755)
    builder = _bare_name_builder(probe.name)

    replaced = builder().run_sync(
        context=ExecutionContext(
            env={"PATH": str(bindir)},
            env_mode=EnvMode.REPLACE,
        )
    )

    assert replaced.exit_code == 0, (
        "a replacement PATH naming the program's directory must resolve a bare "
        f"name, got exit {replaced.exit_code} with stderr {replaced.stderr!r}"
    )
    assert replaced.stdout == "resolved-from-replacement-path\n", (
        "the resolved program must be the one the replacement PATH names"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX bare-name PATH resolution")
def test_replacement_without_path_cannot_resolve_a_bare_program_name(
    tmp_path: Path,
) -> None:
    """Dropping ``PATH`` from a replacement policy makes the spawn itself fail.

    This is the boundary the metrics adapter documents: the failure is raised
    before ``start``, so no ``exit`` event and no ``cuprum_failures_total``
    sample follow. Pinning it here keeps that documented gap honest — if a
    later change ever teaches the spawn path to fall back to the live
    environment, this test fails rather than silently papering over the
    documented absence of a failure sample.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    probe = bindir / "cuprum-probe-unresolvable"
    probe.write_text("#!/bin/sh\necho should-never-run\n")
    probe.chmod(0o755)
    builder = _bare_name_builder(probe.name)

    with pytest.raises(FileNotFoundError):
        builder().run_sync(context=ExecutionContext(env={}, env_mode=EnvMode.REPLACE))


def test_observed_events_carry_the_resolved_environment_mode(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Every observed phase carries the mode the execution actually ran under.

    The observation tag is a separate surface from the typed field: the tag is
    grafted from the same resolved policy, so the tag tests in
    ``test_stage_observation_builder`` stay green if the builder stops passing
    that policy on to :class:`~cuprum._pipeline_types._StageObservation`. Only
    the emitted event can witness the two staying in step, so this asserts the
    field on a real run rather than on a hand-built event.
    """
    events: list[ExecEvent] = []

    def hook(ev: ExecEvent) -> None:
        """Record an emitted execution event."""
        events.append(ev)

    command = python_builder("-c", "print('observed')")
    with sh.observe(hook):
        command.run_sync()
        boundary = len(events)
        command.run_sync(
            context=ExecutionContext(
                env={"CUPRUM_TEST_OBSERVE_MODE": "replaced"},
                env_mode=EnvMode.REPLACE,
            )
        )

    overlay_events = events[:boundary]
    replace_events = events[boundary:]

    assert overlay_events, "the ordinary run must emit observe events"
    assert replace_events, "the replacement run must emit observe events"
    assert all(ev.env_mode is EnvMode.OVERLAY for ev in overlay_events), (
        f"an ordinary run must report overlay, got "
        f"{[ev.env_mode for ev in overlay_events]!r}"
    )
    assert all(ev.env_mode is EnvMode.REPLACE for ev in replace_events), (
        f"a replacement run must report replace on every phase, got "
        f"{[ev.env_mode for ev in replace_events]!r}"
    )
    assert {ev.phase for ev in replace_events} >= {"plan", "start", "exit"}, (
        "the replacement run must span the lifecycle phases, not just one"
    )


def test_execution_context_keeps_existing_positional_slots() -> None:
    """Adding an environment mode leaves legacy positional calls intact."""
    context = ExecutionContext(None, "legacy-cwd")

    assert context.cwd == "legacy-cwd", "cwd must retain its positional slot"
    assert context.env_mode is EnvMode.OVERLAY, (
        "new positional calls must retain the default overlay mode"
    )


def test_cuprum_context_keeps_restriction_marker_positional_slot() -> None:
    """The internal restriction marker retains its established field position."""
    is_restricted = True
    context = CuprumContext(frozenset(), (), (), (), None, None, is_restricted)

    assert context._allowlist_is_restricted is True, (
        "the legacy positional argument must remain the restriction marker"
    )
    assert context.env_mode is EnvMode.OVERLAY, (
        "the new environment mode must retain its default value"
    )


def test_env_registration_keeps_legacy_overlay_default() -> None:
    """Direct registration construction retains the overlay policy default."""
    original = current_context()
    registration = EnvRegistration({"CUPRUM_TEST_DIRECT_REGISTRATION": "value"})

    try:
        context = current_context()
        assert context.env_mode is EnvMode.OVERLAY, (
            "direct registration must retain the overlay policy default"
        )
        assert context.env_overlay == {"CUPRUM_TEST_DIRECT_REGISTRATION": "value"}, (
            "direct registration must install its supplied overlay"
        )
    finally:
        registration.detach()

    assert current_context() is original, (
        "detaching a direct registration must restore the original context"
    )
