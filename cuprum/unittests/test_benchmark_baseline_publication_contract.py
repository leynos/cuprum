"""Contract tests for how `ci.yml` publishes and reads the baseline window.

The statistics in `benchmarks/ratchet_history.py` and the re-measurement in
`benchmarks/confirm_regression.py` only describe reality if the workflow
feeds them every main-branch sample, reads every one back, and measures a
second time before failing. A handful of declarations decide that, and each
fails silently in the direction that reinstates what this work removed:

- gate the recording step on the ratchet passing, and the window fills with
  measurements that were only kept because they beat the bar;
- leave the artefact fetch filtering on `status=success`, and the same
  samples are dropped a step later instead, because a run that failed its
  own ratchet is not a successful run;
- stop passing `--baseline-history` and the comparison silently falls back
  to the single sample this work replaced, with nothing failing; and
- let the re-measurement write under the candidate prefix, and the sample
  recorded into the window becomes the confirming run rather than the
  primary one — but only for the merges that were about to fail, which is a
  verdict-dependent bias in the samples all over again.

None of that is observable from the benchmark code, so these tests read the
workflow. They share `tests.helpers.workflow` with the path-gate contract
tests, which assert a different property of the same job.
"""

from __future__ import annotations

from tests.helpers.workflow import (
    BENCHMARK_JOB,
    CI_WORKFLOW,
    mapping,
    script_of,
    step_named,
)

RECORD_STEP = "Record this run's benchmark sample"
UPLOAD_STEP = "Upload main benchmark baseline artifact"
FETCH_STEP = "Fetch latest main benchmark baseline"
BENCHMARK_STEP = "Run throughput benchmarks and ratchet comparison"
ARTEFACT_CHECK_STEP = "Check candidate benchmark artefacts"

#: The condition both main-branch publication steps must carry. `always()`
#: would publish from a cancelled, half-measured run; plain `success()` —
#: what GitHub infers when no status function is named — would publish only
#: from runs that passed, which is the bias itself.
PUBLICATION_CONDITION = (
    "${{ !cancelled() && github.event_name == 'push' "
    "&& github.ref == 'refs/heads/main' "
    "&& steps.candidate-artefacts.outputs.available == 'true' }}"
)


def test_every_main_run_records_its_sample() -> None:
    """The recording step must not be conditioned on the ratchet's verdict.

    A window fed only by passing runs is a window of low-biased samples: a
    measurement faster than the bar is always accepted, and the slower
    measurements that would correct it are the ones rejected.
    """
    condition = step_named(BENCHMARK_JOB, RECORD_STEP).get("if")

    assert condition == PUBLICATION_CONDITION, (
        f"the {RECORD_STEP!r} step must run on every completed main push — "
        f"{PUBLICATION_CONDITION} — so a run that measured a slowdown still "
        f"records it; found {condition!r}"
    )


def test_the_baseline_artefact_is_published_from_every_main_run() -> None:
    """Publishing only from passing runs discards the corrective samples."""
    condition = step_named(BENCHMARK_JOB, UPLOAD_STEP).get("if")

    assert condition == PUBLICATION_CONDITION, (
        f"the {UPLOAD_STEP!r} step must publish on every completed main push; "
        f"found {condition!r}"
    )


def test_the_published_artefact_carries_the_window() -> None:
    """The artefact must contain the history file, not just the latest run."""
    inputs = mapping(
        step_named(BENCHMARK_JOB, UPLOAD_STEP).get("with"),
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
    script = script_of(step_named(BENCHMARK_JOB, FETCH_STEP))
    assert script is not None, f"the {FETCH_STEP!r} step must run a script"

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
    script = script_of(step_named(BENCHMARK_JOB, BENCHMARK_STEP))
    assert script is not None, f"the {BENCHMARK_STEP!r} step must run a script"

    assert "--baseline-history" in script, (
        f"{CI_WORKFLOW} must pass --baseline-history to the ratchet; without "
        "it the comparison silently uses the single-sample fallback"
    )


def test_a_reported_regression_is_measured_again_before_it_fails() -> None:
    """The failure path must re-measure and combine, not just report.

    Without the second measurement a single unlucky runner fails the job and
    only a human pressing re-run can undo it.
    """
    script = script_of(step_named(BENCHMARK_JOB, BENCHMARK_STEP))
    assert script is not None, f"the {BENCHMARK_STEP!r} step must run a script"

    assert "confirm_regression.py" in script, (
        "the benchmark step must combine the two measurements through "
        f"confirm_regression.py. Found:\n{script}"
    )
    assert "ratchet-report-confirmation.json" in script, (
        "the confirming comparison must write its own report, so the "
        "combined verdict has two verdicts to intersect"
    )


def test_the_re_measurement_does_not_overwrite_the_recorded_sample() -> None:
    """The confirmation run must write under its own prefix.

    `candidate-plan.json` is what the main-branch recorder reads. Letting the
    re-measurement overwrite it would put the confirming run into the window
    instead of the primary one, and only for the merges that were about to
    fail — reintroducing a verdict-dependent bias in the samples.
    """
    script = script_of(step_named(BENCHMARK_JOB, BENCHMARK_STEP))
    assert script is not None, f"the {BENCHMARK_STEP!r} step must run a script"

    assert 'run_smoke_benchmarks "${GITHUB_WORKSPACE}" "confirmation"' in script, (
        "the re-measurement must use the 'confirmation' prefix so "
        f"candidate-* stays the primary measurement. Found:\n{script}"
    )


def test_the_sample_is_staged_before_the_comparison_can_fail() -> None:
    """The files the artefact publishes must exist even when the ratchet fails.

    The comparison exits non-zero on a regression, and `set -e` ends the
    step there. Copying the candidate afterwards would mean a regressed run
    published nothing — the exact sample the window most needs.
    """
    script = script_of(step_named(BENCHMARK_JOB, BENCHMARK_STEP))
    assert script is not None, f"the {BENCHMARK_STEP!r} step must run a script"
    staged = script.index("main-plan.json")
    compared = script.index("ratchet_rust_performance.py")

    assert staged < compared, (
        "the candidate must be copied to main-plan.json before the ratchet "
        "runs, so a failing comparison still leaves a publishable sample"
    )


def test_publication_requires_candidate_measurement_artefacts() -> None:
    """Both publication steps must use the same checked artefact availability."""
    check_script = script_of(step_named(BENCHMARK_JOB, ARTEFACT_CHECK_STEP))
    assert check_script is not None, (
        f"the {ARTEFACT_CHECK_STEP!r} step must run a script"
    )

    for filename in ("candidate-plan.json", "candidate-throughput.json"):
        assert filename in check_script, (
            f"the artefact check must require {filename!r}; found:\n{check_script}"
        )
