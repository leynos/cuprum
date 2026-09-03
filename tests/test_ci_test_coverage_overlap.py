"""Contracts recording which CI jobs are, and are not, subsumed by coverage.

The estate rule is that a test-only job should not run beside a coverage job
when it adds nothing over it. Applying that rule to Cuprum removes nothing, and
these tests pin the three facts that make the answer "nothing" rather than
leaving it to be re-derived, or worse, assumed the other way by someone
deleting a gate that looks redundant.

The measured comparison is recorded in the developers' guide. In short: the
coverage lane detects Cuprum as a Python project, so it runs no Rust tests at
all, and it runs without the compiled extension, so the extension-gated modules
skip inside it.
"""

from __future__ import annotations

import pytest

from tests.helpers.ci_runners import (
    GENERATE_COVERAGE,
    ROOT,
    step_inputs,
    steps,
)

#: The two jobs that run the shared coverage action.
COVERAGE_JOBS = (("ci.yml", "coverage"), ("coverage-main.yml", "coverage-upload"))
#: Inputs the coverage action may receive. `language` is absent deliberately:
#: detection must stay automatic, because pinning it to `mixed` would silently
#: change which suites the lane executes and therefore this whole analysis.
ALLOWED_COVERAGE_INPUTS = frozenset({
    "format",
    "output-path",
    "pytest-workers",
    "cache-provider",
    "with-ratchet",
})


@pytest.mark.parametrize(("workflow_name", "job_name"), COVERAGE_JOBS)
def test_the_coverage_lane_stays_python_only(workflow_name: str, job_name: str) -> None:
    """Pin the detection inputs that decide which suites coverage executes.

    The shared action classifies a project by its repository-root manifest.
    Cuprum's Rust workspace lives under `rust/`, so the root has no
    `Cargo.toml` and the action detects `python`: it skips `cargo-llvm-cov`,
    `cargo nextest`, and its Rust coverage script entirely. Nothing may pin
    `language` to override that without revisiting which jobs it subsumes.
    """
    assert not (ROOT / "Cargo.toml").exists(), (
        "a repository-root Cargo.toml would make the coverage action detect a "
        "mixed project and start running the Rust suite, which changes which "
        "jobs it subsumes; revisit the analysis in the developers' guide"
    )
    coverage_step = next(
        step
        for step in steps(workflow_name, job_name)
        if step.get("uses") == GENERATE_COVERAGE
    )
    declared = set(
        step_inputs(coverage_step, f"{workflow_name}:{job_name} coverage inputs")
    )
    unexpected = sorted(declared - ALLOWED_COVERAGE_INPUTS)
    assert not unexpected, (
        f"{workflow_name}:{job_name} passes {unexpected} to the coverage "
        "action; a new input may change the suites it runs"
    )


def test_the_rust_suite_keeps_a_dedicated_gate() -> None:
    """Keep the Rust tests gated, since the coverage lane never runs them.

    `typecheck-test` is the only job that executes the Rust suite. Its test
    step must keep invoking the Make target that runs nextest; dropping the
    step as "already covered" would silently stop running 104 Rust tests.
    """
    script = next(
        step["run"]
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Run tests"
    )
    assert isinstance(script, str), "ci.yml:typecheck-test must run a test script"
    assert "make test" in script, (
        "ci.yml:typecheck-test must keep invoking `make test`, the only "
        "execution of the Rust suite anywhere in CI"
    )
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    test_target = makefile.split("\ntest:", 1)[1].split("\n\n", 1)[0]
    assert "nextest run" in test_target, (
        "`make test` must keep running the Rust suite; the coverage lane is "
        "Python-only and cannot gate it"
    )


def test_the_extension_gate_builds_the_extension_first() -> None:
    """Record why `extension-tests` is not subsumed by the coverage lane.

    Coverage runs without the compiled extension, so the extension-gated
    modules skip inside it: the pull-request run logged its `rust-backend`
    cases as SKIPPED. Only this job builds the extension and then fails when it
    is absent, so only this job actually exercises the Python/Rust boundary.
    """
    names = [str(step.get("name", "")) for step in steps("ci.yml", "extension-tests")]
    assert "Build the native extension" in names, (
        "ci.yml:extension-tests must build the extension, which is the whole "
        "reason the coverage lane does not subsume it"
    )
    build_index = names.index("Build the native extension")
    test_index = names.index("Run extension-gated tests")
    assert build_index < test_index, (
        "ci.yml:extension-tests must build the extension before running the "
        "gated modules, or they skip exactly as they do under coverage"
    )
    coverage_names = [str(step.get("name", "")) for step in steps("ci.yml", "coverage")]
    assert "Build the native extension" not in coverage_names, (
        "if the coverage lane ever builds the extension, it starts covering "
        "the boundary and the subsumption analysis must be redone"
    )
