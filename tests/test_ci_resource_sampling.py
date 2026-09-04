"""Contract tests for the resource evidence Cuprum's paid Linux jobs leave.

`ubicloud-standard-2` carries roughly 31 GB free at job start, and the failure
mode when that runs out is a silent death rather than a diagnostic. Nothing in
a green run says whether the shape was nearly the constraint, so every job that
compiles or runs a suite records its peaks, and the coverage jobs discard the
instrumented tree before any archive can carry it.
"""

from __future__ import annotations

import typing as typ

from tests.helpers.ci_runners import UBICLOUD_JOBS, expand, step_inputs, steps

RESOURCE_SAMPLER = "./.github/actions/resource-sampler"
#: Paid Linux jobs whose peak memory and disk are worth recording: every one
#: that compiles or runs a suite. The two wheel jobs in `build-wheels.yml`
#: unpack an artefact someone else built and have no high-water mark to find.
SAMPLED_JOBS: typ.Final = (
    ("ci.yml", "typecheck-test"),
    ("ci.yml", "extension-tests"),
    ("ci.yml", "coverage"),
    ("ci.yml", "benchmark-ratchet"),
    ("coverage-main.yml", "coverage-upload"),
)
#: Jobs that build an instrumented tree, which is scratch the moment its report
#: is written.
COVERAGE_JOBS: typ.Final = (
    ("ci.yml", "coverage"),
    ("coverage-main.yml", "coverage-upload"),
)


def test_every_ubicloud_job_measures_its_resource_use() -> None:
    """Make a runner-shape decision citable from the log rather than inferred.

    `ubicloud-standard-2` carries roughly 31 GB free at job start, and the
    failure mode when that runs out is a silent death, not a diagnostic. A job
    with no sampler leaves nobody able to say afterwards whether the shape was
    the constraint, so the peaks are recorded on every paid Linux job.
    """
    for workflow_name, job_name in expand(UBICLOUD_JOBS):
        job_steps = steps(workflow_name, job_name)
        # Collected as a list first. Two `start` steps would each leave a
        # background sampler running while the single `report` kills only the
        # most recent, and a mapping built directly would hide the duplicate.
        message = f"{workflow_name}:{job_name} sampler"
        sampler_steps = [
            (str(step_inputs(step, message).get("mode")), index, step)
            for index, step in enumerate(job_steps)
            if step.get("uses") == RESOURCE_SAMPLER
        ]
        sampler = {mode: (index, step) for mode, index, step in sampler_steps}
        if not sampler_steps:
            # Jobs that neither compile nor test have no high-water mark worth
            # sampling; the manifest below names the ones that do.
            assert (workflow_name, job_name) not in SAMPLED_JOBS, (
                f"{workflow_name}:{job_name} must sample its resource use"
            )
            continue
        assert len(sampler_steps) == len(sampler), (
            f"{workflow_name}:{job_name} declares a sampler mode twice; each "
            "extra start leaves a sampler the single report cannot kill"
        )
        assert set(sampler) == {"start", "report"}, (
            f"{workflow_name}:{job_name} must both start and report the sampler"
        )
        start_index, _ = sampler["start"]
        report_index, report = sampler["report"]
        assert start_index < report_index, (
            f"{workflow_name}:{job_name} must start the sampler before reporting"
        )
        assert report_index == len(job_steps) - 1, (
            f"{workflow_name}:{job_name} must report last, so the peak covers "
            "every build and every cache save"
        )
        assert report.get("if") == "always()", (
            f"{workflow_name}:{job_name} must report the peak even when the "
            "job failed, because disk exhaustion is what the sampler is for"
        )


def test_coverage_jobs_discard_the_instrumented_tree_before_saving() -> None:
    """Keep scratch out of the archives and out of the measured peak."""
    for workflow_name, job_name in COVERAGE_JOBS:
        job_steps = steps(workflow_name, job_name)
        discard = next(
            (
                index
                for index, step in enumerate(job_steps)
                if step.get("name") == "Discard the instrumented build tree"
            ),
            None,
        )
        assert discard is not None, (
            f"{workflow_name}:{job_name} must discard `target/llvm-cov-target`: "
            "it has no consumer once the report is written and it is the second "
            "tree on the volume"
        )
        script = job_steps[discard].get("run")
        assert isinstance(script, str), (
            f"{workflow_name}:{job_name} discard step must run a script"
        )
        assert script.count("df -h") == 2, (
            f"{workflow_name}:{job_name} must print `df -h` either side of the "
            "deletion, so the reclaim is a measurement rather than a claim"
        )
        # Run 33857764655 removed nothing while reporting success, because the
        # tree is built beside `rust/Cargo.toml` and the step named a
        # repository-root path. Searching for it cannot go stale that way.
        assert "find ." in script, (
            f"{workflow_name}:{job_name} must locate the instrumented tree "
            "rather than name a path that moves with the Cargo manifest"
        )
        saves = [
            index
            for index, step in enumerate(job_steps)
            if str(step.get("name", "")).startswith("Save the ")
        ]
        assert all(discard < index for index in saves), (
            f"{workflow_name}:{job_name} must discard the tree before every "
            "save, so no archive carries it"
        )
