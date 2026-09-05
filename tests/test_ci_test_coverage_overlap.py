"""Contracts for the rule that each suite executes once per event.

A suite that runs twice costs runner minutes twice and gates nothing extra. The
rule this module pins is that the coverage job is the *only* place the Rust
suite runs, and that no interpreter runs the Python suite both there and in the
matrix.

None of it is visible from a green run: a workflow that runs the Rust tests
twice, or that stops running them at all because the coverage lane silently
detected a Python-only project, still reports success. The reasoning behind
each assertion is recorded under "One execution per suite" in the developers'
guide.
"""

from __future__ import annotations

import re
import typing as typ

import pytest

from tests.helpers.ci_runners import (
    GENERATE_COVERAGE,
    ROOT,
    expand,
    job,
    step_inputs,
    steps,
)

if typ.TYPE_CHECKING:
    from tests.helpers.workflow_types import Step

#: The jobs that run the shared coverage action, one per event type.
COVERAGE_JOBS = (("ci.yml", "coverage"), ("coverage-main.yml", "coverage-upload"))
#: Inputs that make the shared action execute the Rust suite. Detection alone
#: would not: Cuprum's workspace lives under `rust/`, so the repository root has
#: no `Cargo.toml` and the action would classify the project as Python-only and
#: skip the Rust tests entirely.
RUST_EXECUTION_INPUTS: typ.Final = {
    "language": "mixed",
    "cargo-manifest": "rust/Cargo.toml",
    "all-targets": "true",
    "all-features": "true",
    "doctests": "true",
}
#: The interpreter the coverage job runs pytest on. The matrix leg for this
#: version must not run the Python suite as well.
COVERED_PYTHON_VERSION = "3.13"
#: The exact expression a runner-bounded env value must carry.
VCPU_EXPRESSION = "${{ env.LINUX_RUNNER_VCPUS }}"
#: Commands that execute the Rust suite. `make test-python` and
#: `make test-extension` are absent by construction: `\btest\b(?!-)` does not
#: match a hyphenated target, so the allowed ones need no special case.
FORBIDDEN_TEST_COMMANDS = (
    re.compile(r"\bmake\s+test\b(?!-)"),
    re.compile(r"\bmake\s+test-rust\b"),
    re.compile(r"\bnextest\s+run\b"),
    re.compile(r"\bcargo\s+test\b"),
)


def _coverage_step(workflow_name: str, job_name: str) -> Step:
    """Return the shared coverage action step declared by one job.

    Parameters
    ----------
    workflow_name : str
        File name of the workflow under ``.github/workflows``.
    job_name : str
        Identifier of the coverage job.

    Returns
    -------
    Step
        The step invoking the shared coverage action.
    """
    matches = [
        step
        for step in steps(workflow_name, job_name)
        if str(step.get("uses", "")).startswith(f"{GENERATE_COVERAGE}@")
    ]
    # Exactly one, not merely a first. A second invocation would run the whole
    # suite again, and checking only the first would report that as clean.
    assert len(matches) == 1, (
        f"{workflow_name}:{job_name} must invoke the coverage action exactly "
        f"once, found {len(matches)}"
    )
    return matches[0]


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_coverage_executes_the_rust_suite(workflow_name: str, job_name: str) -> None:
    """Keep the Rust suite running, and running under instrumentation."""
    inputs = step_inputs(
        _coverage_step(workflow_name, job_name),
        f"{workflow_name}:{job_name} coverage must declare inputs",
    )
    wrong = {
        name: inputs.get(name)
        for name, expected in RUST_EXECUTION_INPUTS.items()
        if inputs.get(name) != expected
    }
    assert not wrong, (
        f"{workflow_name}:{job_name} is the only execution of the Rust suite, "
        f"so it must pass {RUST_EXECUTION_INPUTS}; got {wrong}"
    )


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_coverage_keeps_the_warnings_posture_and_core_budget(
    workflow_name: str, job_name: str
) -> None:
    """Carry the flags the uninstrumented run enforced into the surviving one."""
    environment = _coverage_step(workflow_name, job_name).get("env")
    assert isinstance(environment, dict), (
        f"{workflow_name}:{job_name} coverage must declare env"
    )
    assert environment.get("RUSTFLAGS") == "-D warnings", (
        f"{workflow_name}:{job_name} must keep denying warnings now that this "
        "is the only Rust execution"
    )
    for name in ("CARGO_BUILD_JOBS", "NEXTEST_TEST_THREADS"):
        # The whole expression, not a substring of it: `NOT_LINUX_RUNNER_VCPUS`
        # contains the constant's name and would satisfy a containment check
        # while binding something else entirely.
        assert environment.get(name) == VCPU_EXPRESSION, (
            f"{workflow_name}:{job_name} must bound {name} by {VCPU_EXPRESSION}, "
            f"got {environment.get(name)!r}"
        )


def test_no_other_job_executes_the_rust_suite() -> None:
    """Fail if an uninstrumented Rust run reappears anywhere.

    `make test` still runs both suites, which is what a contributor wants
    locally. CI must call `make test-python`, never `make test`, outside the
    coverage jobs.
    """
    offenders: list[str] = []
    for workflow_name, job_name in expand({
        "ci.yml": ("lint-test", "typecheck-test", "extension-tests"),
        "coverage-main.yml": (),
    }):
        for step in steps(workflow_name, job_name):
            script = str(step.get("run", ""))
            # Each forbidden command is matched on its own. Skipping the rest
            # of a script because it also contains an allowed target would let
            # `make test-python && make test` through.
            hits = [
                pattern.pattern
                for pattern in FORBIDDEN_TEST_COMMANDS
                if pattern.search(script)
            ]
            if hits:
                offenders.append(
                    f"{workflow_name}:{job_name}:{step.get('name')} {hits}"
                )
    assert not offenders, (
        "the coverage job is the only execution of the Rust suite; these run "
        f"it again uninstrumented: {offenders}"
    )


def test_the_covered_interpreter_does_not_repeat_the_python_suite() -> None:
    """Run pytest once per interpreter, counting the coverage job's run."""
    matrix_job = job("ci.yml", "typecheck-test")
    strategy = matrix_job.get("strategy")
    assert isinstance(strategy, dict), "typecheck-test must declare a strategy"
    matrix = strategy.get("matrix")
    assert isinstance(matrix, dict), "typecheck-test must declare a matrix"
    include = matrix.get("include")
    assert isinstance(include, list), "typecheck-test must list its legs"
    # Every leg, not a mapping keyed by version: two legs naming the same
    # interpreter would collapse, and the survivor could report `false` while
    # the other one ran the suite.
    wrong = [
        leg
        for leg in include
        if leg["python-suite"] is not (leg["python-version"] != COVERED_PYTHON_VERSION)
    ]
    assert not wrong, (
        f"every leg on {COVERED_PYTHON_VERSION} must set python-suite false, "
        f"because the coverage job already runs pytest there, and every other "
        f"leg must set it true; wrong legs: {wrong}"
    )
    covered = [
        leg for leg in include if leg["python-version"] == COVERED_PYTHON_VERSION
    ]
    assert len(covered) == 1, (
        f"expected exactly one {COVERED_PYTHON_VERSION} leg, found {len(covered)}"
    )
    run_step = next(
        step
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Run tests"
    )
    assert run_step.get("if") == "matrix.python-suite", (
        "the test step must be gated on the matrix flag rather than on a "
        "version literal, so adding an interpreter cannot silently duplicate it"
    )


def test_the_extension_gate_is_not_a_duplicate_run() -> None:
    """Record why `extension-tests` survives the deduplication.

    It runs the same interpreter as the coverage job, but with the compiled
    extension present. Coverage runs without it and the gated modules skip
    there; run 33752095108 logged its `rust-backend` cases as SKIPPED. The two
    runs therefore execute different code.

    Selection is by what the steps run, not by what they are called. A step
    labelled "Build the native extension" that no longer builds it would
    otherwise satisfy this contract.
    """
    scripts = [str(step.get("run", "")) for step in steps("ci.yml", "extension-tests")]
    builds = [index for index, run in enumerate(scripts) if "make develop" in run]
    gated = [index for index, run in enumerate(scripts) if "make test-extension" in run]
    assert len(builds) == 1, (
        "ci.yml:extension-tests must build the extension exactly once, "
        f"found {len(builds)} steps running `make develop`"
    )
    assert len(gated) == 1, (
        "ci.yml:extension-tests must run the gated modules exactly once, "
        f"found {len(gated)} steps running `make test-extension`"
    )
    assert builds[0] < gated[0], (
        "the extension must be built before the gated modules run, or they "
        "skip exactly as they do under coverage"
    )
    coverage_scripts = [
        str(step.get("run", "")) for step in steps("ci.yml", "coverage")
    ]
    assert not any("make develop" in run for run in coverage_scripts), (
        "if the coverage job ever builds the extension it starts covering the "
        "boundary, and `extension-tests` becomes a duplicate run"
    )


def test_make_keeps_both_suites_available_locally() -> None:
    """Keep `make test` running everything, whatever CI splits apart."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "\ntest: test-python test-rust" in makefile, (
        "`make test` must still run both suites for contributors, even though "
        "CI calls the halves separately"
    )
    rust_target = makefile.split("\ntest-rust:", 1)[1].split("\n\n", 1)[0]
    assert "nextest run" in rust_target, (
        "`make test-rust` must keep running the Rust suite"
    )
