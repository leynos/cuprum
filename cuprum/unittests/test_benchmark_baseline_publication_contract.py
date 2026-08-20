"""Contract tests for how `ci.yml` publishes and reads the baseline window.

The statistics in `benchmarks/ratchet_history.py` only describe reality if
the workflow feeds them every main-branch sample and reads every one back.
Three declarations decide that, and each fails silently in the direction
that reinstates the bias the window exists to remove:

- gate the recording step on the ratchet passing, and the window fills with
  measurements that were only kept because they beat the bar;
- leave the artefact fetch filtering on `status=success`, and the same
  samples are dropped a step later instead, because a run that failed its
  own ratchet is not a successful run; and
- stop passing `--baseline-history` and the comparison silently falls back
  to the single sample this work replaced, with nothing failing.

None of that is observable from the benchmark code, so these tests read the
workflow. They share `tests.helpers.workflow` with the path-gate contract
tests, which assert a different property of the same job.
"""

from __future__ import annotations

from tests.helpers.workflow import BENCHMARK_JOB, CI_WORKFLOW, mapping, steps

RECORD_STEP = "Record this run's benchmark sample"
UPLOAD_STEP = "Upload main benchmark baseline artifact"
FETCH_STEP = "Fetch latest main benchmark baseline"

#: The condition both main-branch publication steps must carry. `always()`
#: would publish from a cancelled, half-measured run; plain `success()` —
#: what GitHub infers when no status function is named — would publish only
#: from runs that passed, which is the bias itself.
PUBLICATION_CONDITION = (
    "${{ !cancelled() && github.event_name == 'push' "
    "&& github.ref == 'refs/heads/main' }}"
)


def _step_named(name: str) -> dict[str, object]:
    """Return the benchmark job's step with a given `name:`."""
    for step in steps(BENCHMARK_JOB):
        if step.get("name") == name:
            return step
    names = [step.get("name") for step in steps(BENCHMARK_JOB)]
    msg = f"the {BENCHMARK_JOB!r} job must declare a step named {name!r}; found {names}"
    raise AssertionError(msg)


def _script_of(name: str) -> str:
    """Return the `run:` script of a named step."""
    script = _step_named(name).get("run")
    assert isinstance(script, str), f"the {name!r} step must run a script"
    return script


def test_every_main_run_records_its_sample() -> None:
    """The recording step must not be conditioned on the ratchet's verdict.

    A window fed only by passing runs is a window of low-biased samples: a
    measurement faster than the bar is always accepted, and the slower
    measurements that would correct it are the ones rejected.
    """
    condition = _step_named(RECORD_STEP).get("if")

    assert condition == PUBLICATION_CONDITION, (
        f"the {RECORD_STEP!r} step must run on every completed main push — "
        f"{PUBLICATION_CONDITION} — so a run that measured a slowdown still "
        f"records it; found {condition!r}"
    )


def test_the_baseline_artifact_is_published_from_every_main_run() -> None:
    """Publishing only from passing runs discards the corrective samples."""
    condition = _step_named(UPLOAD_STEP).get("if")

    assert condition == PUBLICATION_CONDITION, (
        f"the {UPLOAD_STEP!r} step must publish on every completed main push; "
        f"found {condition!r}"
    )


def test_the_published_artifact_carries_the_window() -> None:
    """The artefact must contain the history file, not just the latest run."""
    inputs = mapping(
        _step_named(UPLOAD_STEP).get("with"),
        f"the {UPLOAD_STEP!r} step must declare inputs",
    )
    paths = str(inputs.get("path", ""))

    assert "main-baseline-history.json" in paths, (
        f"the {UPLOAD_STEP!r} step must upload main-baseline-history.json; "
        "without it every run reads an empty window and falls back to the "
        f"single-sample baseline. Found:\n{paths}"
    )
    assert inputs.get("if-no-files-found") == "error", (
        "publishing nothing must fail loudly: a silently empty baseline "
        "artefact degrades the ratchet one run at a time"
    )


def test_the_fetch_reads_runs_that_failed_their_own_ratchet() -> None:
    """`--run-status completed` is what makes unconditional publication work.

    Publishing from a failing run is pointless while the fetch still asks
    GitHub only for successful ones; the sample would be recorded and then
    never read.
    """
    script = _script_of(FETCH_STEP)

    assert "--run-status" in script, (
        f"the {FETCH_STEP!r} step must pass `--run-status`, or the samples "
        "recorded by failing main runs are dropped at fetch time instead. "
        f"Found:\n{script}"
    )
    assert "completed" in script, (
        f"the {FETCH_STEP!r} step must ask for `completed` runs, not only "
        f"successful ones. Found:\n{script}"
    )


def test_the_comparison_reads_the_window() -> None:
    """The ratchet invocation must pass the history it is meant to judge against."""
    script = _script_of("Run throughput benchmarks and ratchet comparison")

    assert "--baseline-history" in script, (
        f"{CI_WORKFLOW} must pass --baseline-history to the ratchet; without "
        "it the comparison silently uses the single-sample fallback"
    )


def test_the_sample_is_staged_before_the_comparison_can_fail() -> None:
    """The files the artefact publishes must exist even when the ratchet fails.

    The comparison exits non-zero on a regression, and `set -e` ends the
    step there. Copying the candidate afterwards would mean a regressed run
    published nothing — the exact sample the window most needs.
    """
    script = _script_of("Run throughput benchmarks and ratchet comparison")
    staged = script.index("main-plan.json")
    compared = script.index("ratchet_rust_performance.py")

    assert staged < compared, (
        "the candidate must be copied to main-plan.json before the ratchet "
        "runs, so a failing comparison still leaves a publishable sample"
    )
