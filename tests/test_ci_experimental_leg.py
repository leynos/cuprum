"""Contracts for keeping the pre-release leg off pull requests.

The 3.15a leg of `typecheck-test` is `experimental`: it may fail silently and
is not a required check, so on a pull request it could only spend. GitHub
cannot skip one matrix leg, so a job-level flag, `LEG_RUNS`, is false only for
that leg on a pull request, and every step carries it. The leg then starts,
skips every step, and reports success having done nothing.

Three things must hold. The flag must be exactly the expression that means
that, so an inverted flag, which would skip every required leg instead, is
caught. Every step must carry the flag, or the skipped leg still pays for
whichever step forgot it. And the job must still have its steps, because a
rule over "every step" passes on an empty list.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_leg_gate import (
    GATED_LEG_JOB,
    LEG_FLAG_EXPRESSION,
    normalized,
    ungated,
)
from tests.helpers.ci_runners import job, steps

#: Steps the leg must keep. Named rather than counted alone, so deleting the
#: work while keeping the length fails too.
REQUIRED_STEPS: typ.Final = frozenset({
    "Check out repository",
    "Run typechecker",
    "Run tests",
})


def test_the_leg_flag_is_false_only_for_the_experimental_leg_on_a_pull_request() -> (
    None
):
    """The flag is compared whole: inverting it would skip the required legs."""
    workflow_name, job_name = GATED_LEG_JOB
    declared = job(workflow_name, job_name).get("env")
    assert isinstance(declared, dict), f"{job_name} must declare job-level env"
    flag = normalized(declared.get("LEG_RUNS"))
    assert flag == LEG_FLAG_EXPRESSION, (
        f"{job_name} must set LEG_RUNS to {LEG_FLAG_EXPRESSION!r}, got {flag!r}"
    )


def test_every_step_of_the_leg_carries_the_flag() -> None:
    """A step without the flag still runs, and bills, on the skipped leg."""
    workflow_name, job_name = GATED_LEG_JOB
    ungated_steps = []
    for step in steps(workflow_name, job_name):
        try:
            ungated(workflow_name, job_name, step.get("if"))
        except AssertionError:
            ungated_steps.append(str(step.get("name", step.get("uses"))))
    assert not ungated_steps, f"{job_name} steps without the leg flag: {ungated_steps}"


def test_the_leg_still_has_its_work() -> None:
    """The presence half: an emptied step list satisfies "every step" vacuously."""
    workflow_name, job_name = GATED_LEG_JOB
    names = {str(step.get("name", "")) for step in steps(workflow_name, job_name)}
    missing = sorted(REQUIRED_STEPS - names)
    assert not missing, f"{job_name} must keep its steps; missing {missing}"


def test_exactly_one_leg_is_experimental() -> None:
    """The flag reads `matrix.experimental`, so only the pre-release leg may set it."""
    workflow_name, job_name = GATED_LEG_JOB
    strategy = job(workflow_name, job_name).get("strategy")
    assert isinstance(strategy, dict), f"{job_name} must declare a strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), f"{job_name} must declare a matrix"
    legs = matrix.get("include")
    assert isinstance(legs, list), f"{job_name} must list its legs"
    experimental = [leg.get("python-label") for leg in legs if leg.get("experimental")]
    assert experimental == ["3.15a"], (
        f"only the 3.15a leg may be experimental, got {experimental}"
    )
