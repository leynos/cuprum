"""Contracts for keeping the experimental leg off pull requests, honestly.

The 3.15.2 leg of `typecheck-test` is `experimental`: it may fail silently and
is not a required check, so on a pull request it could only spend. GitHub
cannot skip one matrix leg, so a job-level flag, `LEG_RUNS`, is false only for
that leg on a pull request, and every step carries it. The leg then starts,
skips every step, and reports success having done nothing.

That last conclusion is the defect these contracts grew to cover. A green job
whose steps never ran has been cited as evidence that a pull request was
validated on the experimental interpreter, so the check name itself must say
the leg did not run: `Typecheck and test (Python 3.15.2, not run on pull
requests)`. The 3.12 and 3.14 contexts must stay byte-identical, because they
are required checks.

Four things must hold. The flag must be exactly the expression that means
"skip only the experimental leg on a pull request", so an inverted flag, which
would skip every required leg instead, is caught. Every step must carry the
flag, or the skipped leg still pays for whichever step forgot it. The job must
still have its steps, because a rule over "every step" passes on an empty
list. And the names must render the policy from the matrix entry, so the
suffix cannot leak onto a required context or follow the event.

Two further links carry that policy into effect. The leg's matrix values must
reach `actions/setup-python`, or the action installs its default interpreter
and the version assertions describe a request the runner never made. And the
verification step must reject a mismatched interpreter, or it compares
nothing and every leg passes it.
"""

from __future__ import annotations

import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - runs the checked-in verification program.
import sys
import typing as typ

import pytest

from tests.helpers.ci_job_name import declared_name, rendered_names
from tests.helpers.ci_leg_gate import (
    GATED_LEG_JOB,
    LEG_FLAG_EXPRESSION,
    flag_holds_on,
    normalized,
    ungated,
)
from tests.helpers.ci_leg_matrix import admits_event, matrix_legs
from tests.helpers.ci_runners import job, steps

#: The suffix the experimental entry appends to its check name.
POLICY_SUFFIX: typ.Final = ", not run on pull requests"
#: The label that must equal the exact interpreter request.
EXPERIMENTAL_VERSION: typ.Final = "3.15.2"
#: Every leg the job declares, and the ids a parametrized case reports under.
LEGS: typ.Final = matrix_legs(*GATED_LEG_JOB)
LEG_CASES = [pytest.param(leg, id=str(leg.get("python-label", "?"))) for leg in LEGS]
#: Steps the leg must keep. Named rather than counted alone, so deleting the
#: work while keeping the length fails too.
REQUIRED_STEPS: typ.Final = frozenset({
    "Check out repository",
    "Run typechecker",
    "Verify project interpreter",
    "Run tests",
})
#: The contexts the name renderer must produce for this matrix. Written out
#: rather than derived, because the required 3.12 and 3.14 contexts are what
#: branch protection matches and a derived expectation would follow a rename.
EXPECTED_NAMES: typ.Final = {
    "3.12": "Typecheck and test (Python 3.12)",
    "3.14": "Typecheck and test (Python 3.14)",
    "3.15.2": "Typecheck and test (Python 3.15.2, not run on pull requests)",
}
#: The verification step's embedded program, between its quoted heredoc marks.
#: The quoting is what stops the runner expanding the program's `$` characters,
#: so only a quoted heredoc is accepted.
_HEREDOC: typ.Final = re.compile(r"<<'PY'\n(?P<program>.*?)\nPY\s*\Z", re.DOTALL)


def _step(name: str) -> dict[str, object]:
    """Return the named step of the gated job, refusing a missing one."""
    for step in steps(*GATED_LEG_JOB):
        if step.get("name") == name:
            return typ.cast("dict[str, object]", step)
    message = f"{GATED_LEG_JOB[1]} must keep the step {name!r}"
    raise AssertionError(message)


def _verification_program() -> str:
    """Return the Python program the verification step runs."""
    script = _step("Verify project interpreter").get("run")
    assert isinstance(script, str), "the verification step must run a script"
    found = _HEREDOC.search(script)
    assert found is not None, (
        "the verification step must embed its program in a quoted heredoc, got "
        f"{script!r}"
    )
    return found.group("program")


def _run_verification(program: str, expected: str) -> subprocess.CompletedProcess[str]:
    """Run the verification program with one interpreter expectation."""
    return subprocess.run(
        [sys.executable, "-"],
        capture_output=True,
        check=False,
        env={"EXPECTED_PYTHON": expected},
        input=program,
        text=True,
    )


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
    """The flag reads `matrix.experimental`, so only one entry may set it."""
    experimental = [leg.get("python-label") for leg in LEGS if leg.get("experimental")]
    assert experimental == [EXPERIMENTAL_VERSION], (
        f"only the {EXPERIMENTAL_VERSION} leg may be experimental, got {experimental}"
    )


def test_the_experimental_leg_requests_one_exact_stable_patch() -> None:
    """Pin the version, its label, and the refusal of prereleases together.

    The label is what the check name and the leg's readers spell, so it must
    equal the request rather than approximate it: `3.15a` named a prerelease
    while the request moved on, and the check then described neither.
    """
    experimental = [leg for leg in LEGS if leg.get("experimental")]
    assert len(experimental) == 1, (
        f"one experimental leg expected, got {experimental!r}"
    )
    leg = experimental[0]
    assert leg.get("python-version") == EXPERIMENTAL_VERSION, (
        f"the experimental leg must request exactly {EXPERIMENTAL_VERSION}, got "
        f"{leg.get('python-version')!r}"
    )
    assert leg.get("python-label") == leg.get("python-version"), (
        "the check label must equal the exact interpreter request, so a version "
        f"bump updates both; got label {leg.get('python-label')!r} for "
        f"version {leg.get('python-version')!r}"
    )
    assert leg.get("allow-prereleases") is False, (
        "an exact stable patch must refuse prereleases, or an unpublished "
        "request would install a prerelease of some other patch and validate it"
    )


def test_every_leg_renders_its_exact_check_name() -> None:
    """Pin the contexts branch protection matches, and the policy suffix.

    Rendering rather than reading the template is what makes this a claim
    about the name GitHub displays: an unsupported expression would otherwise
    be compared with itself.
    """
    actual = {
        str(leg.get("python-label")): name
        for leg, name in rendered_names(*GATED_LEG_JOB)
    }
    assert actual == EXPECTED_NAMES, (
        f"the rendered check names must be {EXPECTED_NAMES!r}, got {actual!r}"
    )


def test_only_the_experimental_check_name_states_the_pull_request_policy() -> None:
    """Require the suffix on the skipped leg and nowhere else."""
    for leg, name in rendered_names(*GATED_LEG_JOB):
        has_suffix = POLICY_SUFFIX in name
        assert has_suffix == bool(leg.get("experimental")), (
            f"{name!r} must state the pull-request policy exactly when its leg "
            f"is experimental; leg {leg!r}"
        )


def test_the_check_name_does_not_follow_the_event() -> None:
    """Key the suffix on the matrix entry, not on `github.event_name`.

    An event-dependent name renders differently on a fork's pull request and an
    internal one, so no single required context exists on both.
    """
    template = declared_name(*GATED_LEG_JOB)
    assert "github." not in template.lower(), (
        f"the check name must be stable across events, got {template!r}"
    )


def test_the_validation_steps_bind_uv_to_the_matrix_interpreter() -> None:
    """Keep uv on the requested interpreter, not whichever the cache provides."""
    for name in ("Run typechecker", "Verify project interpreter", "Run tests"):
        declared = _step(name).get("env")
        assert isinstance(declared, dict), f"{name} must declare step-level env"
        assert declared.get("UV_PYTHON") == "${{ matrix.python-version }}", (
            f"{name} must bind uv to the matrix interpreter, got "
            f"{declared.get('UV_PYTHON')!r}"
        )


def test_the_verification_step_proves_the_interpreter_after_typecheck() -> None:
    """Check the environment the suites use, once `.venv` exists.

    `make typecheck` builds the virtual environment, and the tool cache can
    supply one, so the verification has to run after that step and before the
    suite consumes it. Its guard is exactly the leg flag, which is also what
    the every-step contract requires of a gated job's steps.
    """
    workflow_name, job_name = GATED_LEG_JOB
    names = [str(step.get("name", "")) for step in steps(workflow_name, job_name)]
    verification = names.index("Verify project interpreter")
    assert names.index("Run typechecker") < verification, (
        "the verification must follow the step that builds `.venv`"
    )
    assert verification < names.index("Run tests"), (
        "the verification must precede the suite that consumes `.venv`"
    )
    step = _step("Verify project interpreter")
    declared = step.get("env")
    assert isinstance(declared, dict), (
        "the verification step must pass the expectation through env rather "
        "than interpolate it into the shell script"
    )
    assert declared.get("EXPECTED_PYTHON") == "${{ matrix.python-version }}", (
        "the verification must compare against the leg's own request, got "
        f"{declared.get('EXPECTED_PYTHON')!r}"
    )
    assert normalized(step.get("if")) == "env.LEG_RUNS == 'true'", (
        "the verification step's guard must end with the leg flag, got "
        f"{normalized(step.get('if'))!r}"
    )


def test_the_setup_step_requests_the_matrix_interpreter() -> None:
    """The leg's declaration only counts if `actions/setup-python` reads it.

    A step that declares `3.15.2` in the matrix but passes no `python-version`
    to the action installs the action's default, and the version assertions
    would then describe a request the runner never made.
    """
    declared = _step("Set up Python").get("with")
    assert isinstance(declared, dict), "Set up Python must declare with:"
    assert declared.get("python-version") == "${{ matrix.python-version }}", (
        "Set up Python must install the matrix interpreter, got "
        f"{declared.get('python-version')!r}"
    )
    assert declared.get("allow-prereleases") == "${{ matrix.allow-prereleases }}", (
        "Set up Python must honour the leg's prerelease refusal, got "
        f"{declared.get('allow-prereleases')!r}"
    )


def test_the_verification_step_rejects_a_mismatched_interpreter() -> None:
    """Exercise the comparison, not only the placement and environment.

    The step exists to fail a leg whose `.venv` is not the interpreter that
    leg requested. Running its embedded program with an expectation the
    running interpreter cannot satisfy is what shows it does: a program that
    merely printed the version would satisfy every assertion above and never
    fail a leg.
    """
    program = _verification_program()
    matching = f"{sys.version_info.major}.{sys.version_info.minor}"
    accepted = _run_verification(program, matching)
    assert accepted.returncode == 0, (
        f"the verification must accept the running interpreter {matching}, got "
        f"{accepted.returncode}: {accepted.stderr}"
    )
    mismatched = f"{sys.version_info.major + 1}.0"
    rejected = _run_verification(program, mismatched)
    assert rejected.returncode != 0, (
        "the verification must fail a leg whose interpreter is not the one "
        f"requested ({mismatched}), got {rejected.stdout!r}"
    )
    assert "but this leg requested" in rejected.stderr, (
        "a rejected verification must say which interpreter was requested, got "
        f"{rejected.stderr!r}"
    )


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
@pytest.mark.parametrize("leg", LEG_CASES)
def test_every_leg_is_enabled_and_does_its_work_on_trunk_events(
    event: str, leg: dict[str, object]
) -> None:
    """The leg must run in full wherever it is intended to run.

    The flag is asserted for every leg rather than only the experimental one:
    an event clause that switched the required legs off would leave the
    experimental leg's full run looking correct.
    """
    assert flag_holds_on(*GATED_LEG_JOB, leg, event), (
        f"the {leg.get('python-label')} leg must be enabled on {event!r}"
    )
    for name in sorted(REQUIRED_STEPS):
        guard = ungated(*GATED_LEG_JOB, _step(name).get("if"))
        subject = f"{GATED_LEG_JOB[0]}:{GATED_LEG_JOB[1]} step {name!r}"
        assert admits_event(guard, leg, event, subject=subject), (
            f"{name!r} must run on the {leg.get('python-label')} leg for "
            f"{event!r}, got guard {guard!r}"
        )


@pytest.mark.parametrize("leg", LEG_CASES)
def test_a_pull_request_enables_only_the_required_legs(
    leg: dict[str, object],
) -> None:
    """Switch off the experimental leg alone, and leave every other enabled."""
    expected = not leg.get("experimental")
    actual = flag_holds_on(*GATED_LEG_JOB, leg, "pull_request")
    assert actual is expected, (
        f"the {leg.get('python-label')} leg's flag on a pull request must be "
        f"{expected}, got {actual}"
    )
