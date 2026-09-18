"""Contract tests for how wide Cuprum's CI jobs fan out.

A job that asks `pytest-xdist` or Cargo for more workers than the runner has
cores still produces a green suite while thrashing two vCPU, so the worker
counts are derived from one constant that names the shape the job is billed
for. Nothing in a passing run says whether that constant and the label agree.
"""

from __future__ import annotations

import re

from tests.helpers.ci_runners import (
    ROOT,
    UBICLOUD_LABEL,
    UBICLOUD_VCPUS,
    step_inputs,
    steps,
    workflow_env,
    workflow_sources,
)

MAKEFILE = ROOT / "Makefile"
VCPU_CONSTANT = "LINUX_RUNNER_VCPUS"
#: Make variables that carry the runner's vCPU count into the test command.
#: Only the pytest one remains here: the Rust suite moved to the coverage job,
#: which bounds itself through `CARGO_BUILD_JOBS` and `NEXTEST_TEST_THREADS`.
PARALLELISM_OVERRIDES = ("PYTEST_CARGO_BUILD_JOBS",)


def test_the_vcpu_constant_matches_the_assigned_label() -> None:
    """Tie the one parallelism constant to the shape the job is billed for."""
    declared = workflow_env("ci.yml")[VCPU_CONSTANT]
    assert declared == str(UBICLOUD_VCPUS), (
        f"{VCPU_CONSTANT} must equal the vCPU count of {UBICLOUD_LABEL}, "
        f"got {declared!r}"
    )


def test_python_tests_derive_their_worker_counts_from_that_constant() -> None:
    """Size the matrix suite's Cargo work from the constant, not a literal."""
    script = next(
        step["run"]
        for step in steps("ci.yml", "typecheck-test")
        if step.get("name") == "Run tests"
    )
    assert isinstance(script, str), "ci.yml:typecheck-test must run a test script"
    for variable in PARALLELISM_OVERRIDES:
        assert f'{variable}="${{{VCPU_CONSTANT}}}"' in script, (
            f"ci.yml:typecheck-test must pass {variable} from {VCPU_CONSTANT}"
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
