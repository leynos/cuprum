"""What a *bound* stage's fail-fast decision reports, and what it withholds.

`test_pipeline_fail_fast_wiring` runs a failing pipeline whose stages all run
the catalogued interpreter, so no stage carries a resolved path. That leaves
one half of the event contract unexercised: whether a stage that *was* bound
still reports its path on its own lifecycle events while the sanitized decision
event continues to withhold one.

The distinction is not cosmetic. The fail-fast event is the sanitized surface —
it deliberately omits the environment and the resolved path, and a consumer
reading a path from it would be reading a decision about a teardown as though
it named an execution. A regression that copied the failing stage's path onto
the decision event would pass every existing fail-fast test, because none of
them binds the failing stage.

The run binds only the first stage, which is why the support helper gives it a
program of its own: binding the shared interpreter program would bind all three
stages, all would settle in one batch, and the run would emit no decision at
all.
"""

from __future__ import annotations

import stat
import typing as typ

import pytest

from cuprum.unittests._fail_fast_pipeline_support import (
    phase as _phase,
)
from cuprum.unittests._fail_fast_pipeline_support import (
    run_bound_failing_pipeline,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent

_FAILING_PROGRAM = "bound-failing-stage"
# The script the bound stage runs instead of the catalogued interpreter. It must
# exit non-zero, because the pipeline latches a *failing* stage; a zero exit
# would leave nothing to decide about.
_FAILING_SCRIPT = "import sys\nsys.exit(3)\n"


def _write_failing_script(path: Path) -> Path:
    """Write the bound stage's failing executable and make it runnable."""
    path.write_text(
        f"#!/usr/bin/env python3\n{_FAILING_SCRIPT}",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


@pytest.fixture(scope="module")
def bound_failing_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Publish the executable the bound stage runs, for exact-path assertions.

    The events fixture consumes this so both describe the same run, and a test
    that needs to compare a reported path against the real one can do so by
    equality rather than by suffix.

    Returns
    -------
    Path
        The failing script the first stage is bound to.
    """
    return _write_failing_script(tmp_path_factory.mktemp("bound") / "failing.py")


@pytest.fixture(scope="module")
def bound_fail_fast_events(
    bound_failing_script: Path,
) -> tuple[ExecEvent, ...]:
    """Publish one bound fail-fast run to every test in this module.

    The run costs three real subprocesses plus the settling delay the support
    helper pays to reach the decision reliably, so it is shared rather than
    repeated per test. The tuple is what makes the sharing safe: no test can
    leave the next one a shortened or reordered sequence.

    Parameters
    ----------
    bound_failing_script : Path
        Executable the first stage is bound to.

    Returns
    -------
    tuple[ExecEvent, ...]
        Events observed during the bound failing pipeline run.
    """
    return run_bound_failing_pipeline(bound_failing_script)


def test_a_bound_failing_stage_reports_its_path_on_its_own_events(
    bound_fail_fast_events: tuple[ExecEvent, ...],
    bound_failing_script: Path,
) -> None:
    """The bound stage's plan, start, and exit all name the executable it ran.

    Binding and failing are independent facts, and this is the case where they
    meet. A regression that suppressed the resolved path for a stage that
    failed — or resolved it only on the success path — would leave the
    unbound-pipeline tests green, because none of their stages carries a path
    to lose.

    The comparison is equality with the script this module wrote, not a
    filename suffix. A suffix would accept any executable called
    ``failing.py`` — including one the stage was never bound to, which is
    exactly the divergence a path assertion exists to catch.
    """
    bound = [
        event
        for event in bound_fail_fast_events
        if event.program == _FAILING_PROGRAM
        and event.phase in {"plan", "start", "exit"}
    ]

    assert {event.phase for event in bound} == {"plan", "start", "exit"}, (
        f"the bound stage must report its whole lifecycle, got "
        f"{sorted(event.phase for event in bound)!r}"
    )
    expected = str(bound_failing_script)
    for event in bound:
        assert event.resolved_path == expected, (
            f"the bound stage's {event.phase} event must name the executable "
            f"it was bound to, {expected!r}, got {event.resolved_path!r}"
        )


def test_the_decision_event_withholds_the_bound_path(
    bound_fail_fast_events: tuple[ExecEvent, ...],
) -> None:
    """The sanitized decision names the program but never the executable.

    This is the half that a bound run can fail and an unbound one cannot. The
    decision describes a teardown; the failing stage reports its own path on
    the ``exit`` event that follows. A consumer must not be able to read the
    pipeline's decision as having run the bound file, so the path stays unset
    here even though the stage that provoked the decision had one.
    """
    (decision,) = _phase(bound_fail_fast_events, "pipeline_fail_fast")

    assert decision.resolved_path is None, (
        f"the sanitized decision event must withhold the resolved path, "
        f"got {decision.resolved_path!r}"
    )
    assert decision.program == _FAILING_PROGRAM, (
        f"the decision must still name the logical program, got {decision.program!r}"
    )
    assert decision.argv == (), (
        f"the sanitized decision must carry no argument vector, got {decision.argv!r}"
    )


def test_only_the_bound_stage_reports_a_path(
    bound_fail_fast_events: tuple[ExecEvent, ...],
) -> None:
    """The two unbound siblings still run under their catalogued name.

    The negative control for the binding: a regression that resolved *every*
    stage, rather than only the bound one, would give the siblings a path too.
    Without this check the two tests above could both pass while the run
    reported a resolved path for stages that were never bound.
    """
    siblings = [
        event for event in bound_fail_fast_events if event.program != _FAILING_PROGRAM
    ]

    assert siblings, "the run must publish the unbound siblings' events"
    for event in siblings:
        assert event.resolved_path is None, (
            f"an unbound stage's {event.phase} event must carry no resolved path, "
            f"got {event.resolved_path!r}"
        )
