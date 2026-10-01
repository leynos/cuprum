"""Pipeline ``env_mode`` reporting on every stage and the fail-fast decision.

The direct-command lifecycle is already pinned by
``test_env_context_policies``. A pipeline resolves its policy somewhere else
again — once, in ``_build_spawn_observations`` — and hands the one resolved
mode to every stage's observation. Nothing in the suite exercises that route,
so a regression that resolved the policy for the first stage only, or that
stopped passing it to the fail-fast decision event, would leave every existing
pipeline test green.

The two runs below are arranged so their assertions can fail. Each asserts the
child's own view of its environment alongside the reported mode, so a
regression that stopped rendering the replacement boundary is caught by the
same test as one that stopped labelling it.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.catalogue import ProgramCatalogue
from cuprum.context import EnvMode
from cuprum.events import TerminalOutcome
from cuprum.program import Program
from cuprum.sh import ExecutionContext, Pipeline, RunOutputOptions
from cuprum.unittests._fail_fast_pipeline_support import (
    phase,
    run_failing_pipeline,
)
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd

# The phases a stage reports about its own lifecycle, in the order it reports
# them. ``pipeline_fail_fast`` is deliberately absent: it belongs to the
# coordinator, not to any one stage.
_STAGE_LIFECYCLE = ("plan", "start", "exit")

_STAGE_INDEX_TAG = "pipeline_stage_index"

# Both variables matter, and neither is redundant. The inherited one is
# present only in the live parent environment, so it is what distinguishes a
# replacement boundary from an overlay; a variable that the context itself
# supplies would read back under either policy and could not tell them apart.
# The supplied one proves the stage received the context at all.
_INHERITED_VAR = "CUPRUM_TEST_PIPELINE_EVENT_INHERITED"
_SUPPLIED_VAR = "CUPRUM_TEST_PIPELINE_EVENT_SUPPLIED"


def _stage_of(event: ExecEvent) -> int:
    """Return the pipeline stage index an event carries in its tags.

    Returns
    -------
    int
        The ``pipeline_stage_index`` tag. These runs supply no execution tags
        of their own, so the coordinator's value cannot be shadowed here.
    """
    return typ.cast("int", (event.tags or {})[_STAGE_INDEX_TAG])


def _build_two_stage_pipeline(
    python: cabc.Callable[..., SafeCmd],
) -> Pipeline:
    """Build a two-stage pipeline whose consumer samples its own environment.

    The consumer reads stdin and then samples the environment itself rather
    than acting as a pass-through. A consumer that only forwarded the
    producer's output would leave a regression confined to the second stage
    undetected: the producer's report would still look correct.

    Each stage reports the inherited and supplied variables together, so one
    line distinguishes a replacement boundary from an overlay.

    Returns
    -------
    Pipeline
        A pipeline in which both stages report both variables.
    """
    sample = (
        "import os;print('|'.join(os.environ.get(n, '<missing>') for n in "
        f"{(_INHERITED_VAR, _SUPPLIED_VAR)!r}))"
    )
    producer = python("-c", sample)
    consumer = python("-c", f"import sys;print(sys.stdin.read().strip());{sample}")
    return producer | consumer


def _run_replacement_pipeline(
    python: cabc.Callable[..., SafeCmd],
    program: Program,
) -> tuple[list[ExecEvent], str]:
    """Run a two-stage replacement pipeline while observing its events.

    Returns
    -------
    tuple[list[ExecEvent], str]
        The events in publication order, and the run's standard output.
    """
    events: list[ExecEvent] = []
    pipeline = _build_two_stage_pipeline(python)

    with (
        scoped(ScopeConfig(allowlist=frozenset([program]))),
        sh.observe(events.append),
    ):
        result = pipeline.run_sync(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(
                env={_SUPPLIED_VAR: "supplied"},
                env_mode=EnvMode.REPLACE,
            ),
        )

    return events, result.stdout or ""


def test_replacement_pipeline_events_carry_the_mode_on_every_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every stage of a replacement pipeline reports its own mode and result.

    A pipeline spawns each stage from the one policy its observations were
    built with, so a boundary that reached only the first stage would show up
    here as a stage whose child inherited the live environment. Both stages are
    asserted, and the reports are paired with the mode on the events that
    describe the same stage.
    """
    monkeypatch.setenv(_INHERITED_VAR, "inherited")
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)

    events, stdout = _run_replacement_pipeline(python, program)

    assert stdout == "<missing>|supplied\n<missing>|supplied\n", (
        "each stage must discard the live value and keep the supplied one, got "
        f"{stdout!r}"
    )

    stages = {_stage_of(event) for event in events}
    assert stages == {0, 1}, (
        f"the observation stream must cover both stages, found {sorted(stages)}"
    )

    for stage in sorted(stages):
        written = [event for event in events if _stage_of(event) == stage]
        assert {event.phase for event in written} >= set(_STAGE_LIFECYCLE), (
            f"stage {stage} must report its own lifecycle, got "
            f"{sorted({event.phase for event in written})}"
        )
        assert all(event.env_mode is EnvMode.REPLACE for event in written), (
            f"every phase of stage {stage} must report replace, got "
            f"{[event.env_mode for event in written]!r}"
        )


def test_the_mode_tracks_the_policy_rather_than_being_constant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default-policy pipeline reports overlay, so the field tracks policy.

    The same arrangement as the replacement test is run under the default
    policy here. The child keeps the live value under overlay and loses it
    under replacement, so this pair separates the two policies from each other
    as well as from a field that reports a constant.
    """
    monkeypatch.setenv(_INHERITED_VAR, "inherited")
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([program]))),
        sh.observe(events.append),
    ):
        result = _build_two_stage_pipeline(python).run_sync(
            output=RunOutputOptions(capture=True, echo=False),
            context=ExecutionContext(env={_SUPPLIED_VAR: "supplied"}),
        )

    assert (result.stdout or "") == "inherited|supplied\ninherited|supplied\n", (
        f"an overlay pipeline must keep the live value, got {result.stdout!r}"
    )
    assert events, "the default-policy pipeline must emit observe events"
    assert all(event.env_mode is EnvMode.OVERLAY for event in events), (
        f"a default-policy pipeline must report overlay, got "
        f"{[event.env_mode for event in events]!r}"
    )


@pytest.mark.timeout(60)
def test_failing_stage_reports_the_mode_on_the_fail_fast_decision() -> None:
    """The fail-fast decision carries the mode of the run it decided about.

    The decision event is sanitized: it omits the environment, the argv, and
    the resource measurements. The mode survives that sanitization because a
    consumer diagnosing a stage that died on a missing ``PATH`` needs it, and
    it is a bounded policy name with no caller data in it. A regression that
    dropped it would leave the run's teardown unexplainable from events alone.
    """
    events = run_failing_pipeline(
        ExecutionContext(env={_SUPPLIED_VAR: "supplied"}, env_mode=EnvMode.REPLACE)
    )

    fail_fast = phase(events, "pipeline_fail_fast")
    assert len(fail_fast) == 1, (
        f"a failing non-final stage must publish exactly one decision, "
        f"found {len(fail_fast)}"
    )
    (decision,) = fail_fast
    assert decision.env_mode is EnvMode.REPLACE, (
        f"the fail-fast decision must report the run's mode, got {decision.env_mode!r}"
    )
    assert all(event.env_mode is EnvMode.REPLACE for event in events), (
        f"every event of the failing run must agree on the mode, got "
        f"{[event.env_mode for event in events]!r}"
    )


def test_a_stage_that_cannot_spawn_settles_with_error_outcome() -> None:
    """A spawn failure settles without inventing a process or status.

    A replacement policy that omits ``PATH`` leaves a bare program name
    unresolvable, so the stage's spawn raises before its ``start``. The
    coordinator then tears down and re-raises. Running this as a non-final stage
    distinguishes the spawn failure from fail-fast teardown: it must emit one
    ``settled`` event with the error category, while leaving the PID and child
    exit status unset. The effective ``env_mode`` remains available on both
    lifecycle events.
    """
    catalogue, program = python_catalogue()
    python = sh.make(program, catalogue=catalogue)
    absent_program = Program("cuprum-probe-absent-stage")
    absent = sh.make(
        absent_program,
        catalogue=ProgramCatalogue.from_programs(
            absent_program,
            name="absent-stage-probes",
            documentation_locations=("docs/users-guide.md",),
        ),
    )
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([program, absent_program]))),
        sh.observe(events.append),
    ):
        pipeline = python("-c", "print('stage zero')") | absent()
        with pytest.raises(FileNotFoundError):
            pipeline.run_sync(
                output=RunOutputOptions(capture=True, echo=False),
                context=ExecutionContext(env={}, env_mode=EnvMode.REPLACE),
            )

    failing_stage = [event for event in events if _stage_of(event) == 1]
    assert [event.phase for event in failing_stage] == ["plan", "settled"], (
        "a spawn-failed stage must settle after its plan, got "
        f"{[event.phase for event in failing_stage]!r}"
    )
    settled = failing_stage[-1]
    assert settled.terminal_outcome is TerminalOutcome.ERROR, (
        f"a spawn failure must settle as an error, got {settled.terminal_outcome!r}"
    )
    assert settled.pid is None, (
        f"a spawn failure must not invent a process, got pid={settled.pid!r}"
    )
    assert settled.exit_code is None, (
        "a spawn failure must not invent a child status, "
        f"got exit_code={settled.exit_code!r}"
    )
    assert all(event.env_mode is EnvMode.REPLACE for event in failing_stage), (
        "both lifecycle events must retain the policy, got "
        f"{[event.env_mode for event in failing_stage]!r}"
    )
