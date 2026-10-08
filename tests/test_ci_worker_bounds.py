"""Contract tests for how wide Cuprum's CI jobs fan out.

A job that asks `pytest-xdist` or Cargo for more workers than the runner has
cores still produces a green suite while thrashing two vCPU, so the worker
counts are derived from one constant that names the shape the job is billed
for. Nothing in a passing run says whether that constant and the label agree.
"""

from __future__ import annotations

import re
import typing as typ

from tests.helpers.ci_runners import (
    ROOT,
    UBICLOUD_LABEL,
    UBICLOUD_VCPUS,
    step_inputs,
    steps,
    workflow_document,
    workflow_env,
    workflow_sources,
)

MAKEFILE = ROOT / "Makefile"
VCPU_CONSTANT = "LINUX_RUNNER_VCPUS"
#: A Cargo job count handed to a command: a Makefile assignment, a `--jobs`
#: flag, or a `CARGO_BUILD_JOBS` value in a workflow. Cargo defaults to every
#: core, so a pin below the runner's vCPU count only starves a billed runner.
CARGO_JOBS_VALUE = re.compile(
    r"CARGO_BUILD_JOBS\s*(?:[?+!:]?:?=|:)[ \t]*([^\n]*)"
)
#: Any Make assignment to the variable, whatever its operator or modifiers: a
#: caller's `CARGO_BUILD_JOBS` must reach Cargo untouched.
MAKE_CARGO_JOBS_ASSIGNMENT = re.compile(
    r"^[ \t]*(?:(?:override|export)[ \t]+)*CARGO_BUILD_JOBS[ \t]*[?+!:]*:?=",
    re.MULTILINE,
)
SERIAL_CARGO_JOBS = re.compile(r"(?<!pylint )--jobs[ =]1\b")


def test_the_vcpu_constant_matches_the_assigned_label() -> None:
    """Tie the one parallelism constant to the shape the job is billed for."""
    declared = workflow_env("ci.yml")[VCPU_CONSTANT]
    assert declared == str(UBICLOUD_VCPUS), (
        f"{VCPU_CONSTANT} must equal the vCPU count of {UBICLOUD_LABEL}, "
        f"got {declared!r}"
    )


def test_python_tests_derive_their_worker_counts_from_that_constant() -> None:
    """Size the matrix suite's Cargo work from the constant, not a literal.

    `make test-python` sets no job count of its own, so the step hands the
    runner's vCPU count to Cargo through the environment it inherits.
    """
    script = next(
        step["run"]
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Run tests"
    )
    assert isinstance(script, str), "ci.yml:typecheck-test must run a test script"
    assert f'CARGO_BUILD_JOBS="${{{VCPU_CONSTANT}}}" make test-python' in script, (
        f"ci.yml:typecheck-test must pass CARGO_BUILD_JOBS from {VCPU_CONSTANT}"
    )


def test_the_makefile_pins_no_cargo_job_count() -> None:
    """Leave Cargo's job count to the caller and to Cargo's own default.

    A hard-coded single job forces every build onto one core while a billed
    runner sits idle, and it overrides a caller's `CARGO_BUILD_JOBS`.
    `TEST_JOBS` is nextest's test-thread count, not a Cargo build limit, so
    it stays. pylint's `--jobs=1` is not Cargo, so the pattern skips it.
    """
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert not SERIAL_CARGO_JOBS.search(makefile), "no Cargo command may be --jobs 1"
    assert not MAKE_CARGO_JOBS_ASSIGNMENT.search(makefile), (
        "the Makefile must not assign CARGO_BUILD_JOBS; callers own it"
    )
    assert not CARGO_JOBS_VALUE.search(makefile), (
        "no recipe may set CARGO_BUILD_JOBS inline; callers own it"
    )
    for retired in ("PYTEST_CARGO_BUILD_JOBS", "TEST_CARGO_BUILD_JOBS", "DOC_FLAGS"):
        # `RUSTDOC_FLAGS` is a different variable, so match whole words only.
        assert not re.search(rf"\b{retired}\b", makefile), (
            f"{retired} pinned Cargo to one job"
        )


def test_every_workflow_cargo_job_count_derives_from_the_constant() -> None:
    """Refuse a literal `CARGO_BUILD_JOBS`, which pins a runner's cores by hand.

    Each value must name `LINUX_RUNNER_VCPUS`, so raising the label raises the
    count in one place. A workflow with no value uses Cargo's default.
    """
    for path, source in workflow_sources():
        for match in CARGO_JOBS_VALUE.finditer(source):
            assert VCPU_CONSTANT in match.group(1), (
                f"{path}: CARGO_BUILD_JOBS must derive from {VCPU_CONSTANT}, "
                f"got {match.group(1)!r}"
            )


def _cargo_job_values(node: object) -> typ.Iterator[object]:
    """Yield the value of every `CARGO_BUILD_JOBS` key in a parsed workflow."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "CARGO_BUILD_JOBS":
                yield value
            yield from _cargo_job_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _cargo_job_values(item)


def test_every_parsed_env_cargo_job_count_derives_from_the_constant() -> None:
    """Check the parsed `env` mappings too, not only the workflow text.

    The text scan reads shell assignments; this walk reads the YAML, so a
    literal in a job, step, or matrix `env` mapping cannot hide behind
    formatting the pattern does not expect, and a non-string value (`2`) is
    refused as the hand-pinned count it is.
    """
    for path, _ in workflow_sources():
        for value in _cargo_job_values(workflow_document(path)):
            assert isinstance(value, str), (
                f"{path}: CARGO_BUILD_JOBS must be an expression, got {value!r}"
            )
            assert VCPU_CONSTANT in value, (
                f"{path}: CARGO_BUILD_JOBS must derive from {VCPU_CONSTANT}, "
                f"got {value!r}"
            )


def test_extension_and_benchmark_builds_are_bounded_too() -> None:
    """Bound the two jobs that compile outside `make test` to the same count."""
    for job_name, step_name in (
        ("extension-tests", "Build the native extension"),
        ("benchmark-ratchet", "Run throughput benchmarks and ratchet comparison"),
    ):
        script = next(
            step["run"]
            for step in steps("ci.yml", job_name)
            if step.get("name") == step_name
        )
        assert isinstance(script, str), f"ci.yml:{job_name} must run {step_name!r}"
        assert f'CARGO_BUILD_JOBS="${{{VCPU_CONSTANT}}}"' in script, (
            f"ci.yml:{job_name} must bound Cargo build jobs by {VCPU_CONSTANT}"
        )


def test_python_suites_never_ask_for_unbounded_workers() -> None:
    """Reject `-n auto`: the runner has two cores whatever the host reports."""
    sources = [source for _, source in workflow_sources()]
    sources.append(MAKEFILE.read_text(encoding="utf-8"))
    for source in sources:
        assert "-n auto" not in source, "xdist worker counts must be explicit"


def test_the_python_suite_stays_serial() -> None:
    """Keep pytest serial while its batches contend on one Cargo target."""
    makefile = MAKEFILE.read_text(encoding="utf-8")
    assert re.search(r"^PYTEST_WORKERS \?= 0$", makefile, re.MULTILINE), (
        "PYTEST_WORKERS must default to 0; the batches compile and reuse the "
        "same Rust artefacts, so xdist workers would contend on one build lock"
    )
    for workflow_name, job_name in (("ci.yml", "coverage"),):
        coverage_step = next(
            step
            for step in steps(workflow_name, job_name)
            if str(step.get("uses", "")).startswith(
                "leynos/shared-actions/.github/actions/generate-coverage@"
            )
        )
        inputs = step_inputs(coverage_step, f"{workflow_name}:{job_name} inputs")
        workers = inputs.get("pytest-workers")
        assert isinstance(workers, str), (
            f"{workflow_name}:{job_name} coverage must declare pytest-workers"
        )
        assert not workers, (
            f"{workflow_name}:{job_name} coverage must run pytest serially, "
            f"got {workers!r}"
        )
