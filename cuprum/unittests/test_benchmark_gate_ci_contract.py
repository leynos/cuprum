"""Contract tests for the path gate in front of the `benchmark-ratchet` job.

`benchmark-ratchet` is the only paid job in this workflow, so it runs on pull
requests only when the diff can plausibly change throughput. That gate lives
entirely in declarative configuration — a `changes` job running
`dorny/paths-filter`, a `needs` edge, and one `if:` expression — and every
part of it fails silently in the direction that costs money or, worse, hides
a regression:

- invert the condition, or drop a path from the filter, and a genuine
  performance change merges unbenchmarked;
- drop the `github.event_name != 'pull_request'` clause and pushes to `main`
  stop refreshing the baseline artefact that pull-request runs compare
  against, so the ratchet quietly degrades to comparing against nothing;
- move `changes` onto the paid runner and the saving is spent detecting
  whether to spend it.

No ordinary test notices any of that, so these tests parse `ci.yml` and read
the contract back. They pin the *declarations*; the decision those
declarations produce for a given pull request is stated in
`tests/behaviour/test_benchmark_path_gate_behaviour.py`, and both suites read
the workflow through `tests.helpers.workflow`. The build half of the same
workflow's contract — which job builds the extension, and how — lives in
`test_extension_ci_contract.py`.
"""

from __future__ import annotations

import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from tests.helpers.workflow import (
    BENCHMARK_JOB,
    CHANGES_JOB,
    CI_WORKFLOW,
    FILTER_NAME,
    FILTER_STEP_ID,
    bench_output,
    benchmark_gate,
    benchmark_runs,
    filter_paths,
    job,
    mapping,
    step_named,
    step_with_id,
    steps,
    workflow,
)

PATHS_FILTER_ACTION = "dorny/paths-filter@"
SUMMARY_STEP = "Record the benchmark gate decision"

#: The gate, verbatim. Pinning the whole expression rather than probing it for
#: substrings is what makes an inverted or half-deleted condition a failure:
#: `needs.changes.outputs.bench != 'true'` contains every operand the loose
#: check would look for. It is also what keeps `benchmark_runs` — the model the
#: property and behavioural tests reason with — describing the real gate.
EXPECTED_GATE = (
    "needs.changes.result == 'success' && (github.event_name != 'pull_request' || "
    "needs.changes.outputs.bench == 'true')"
)

#: Every path whose contents can change measured throughput: the package and
#: the extension under test, the benchmark harness itself, the dependency and
#: build definitions that decide which code is installed, and this workflow,
#: which decides how the benchmark is run.
EXPECTED_FILTER_PATHS = frozenset({
    "cuprum/**",
    "rust/**",
    "benchmarks/**",
    "conftest.py",
    "Makefile",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/ci.yml",
})

#: Paths a docs-only or workflow-adjacent pull request touches. None of them
#: may match the filter; `test_the_irrelevant_paths_are_genuinely_irrelevant`
#: holds the pool to that, so the property tests below cannot pass by
#: accidentally sampling a performance-relevant path.
IRRELEVANT_PATHS = (
    "README.md",
    "CHANGELOG.md",
    "docs/users-guide.md",
    "docs/execplans/4-4-3-ratchet-rust-performance.md",
    ".github/workflows/release.yml",
    ".github/actionlint.yaml",
)

#: GitHub inserts an implicit `success()` into a job's `if:` unless the
#: expression already names a status function. Naming one here would let
#: `benchmark-ratchet` run when the detector itself failed — that is, run
#: ungated, on the paid runner, which is the failure this gate exists to
#: prevent.
STATUS_FUNCTIONS = ("always(", "failure(", "cancelled(")


def test_the_changes_job_publishes_the_filter_result() -> None:
    """`changes` must expose the filter's verdict as its `bench` output.

    The output is the whole interface between the two jobs; without it the
    gate reads an empty string and every pull request skips the benchmark.
    """
    outputs = mapping(
        job(CHANGES_JOB).get("outputs"),
        f"the {CHANGES_JOB!r} job must declare outputs",
    )

    assert outputs.get(FILTER_NAME) == (
        f"${{{{ steps.{FILTER_STEP_ID}.outputs.{FILTER_NAME} }}}}"
    ), (
        f"the {CHANGES_JOB!r} job must publish its {FILTER_NAME!r} output from the "
        f"{FILTER_STEP_ID!r} step; found {outputs.get(FILTER_NAME)!r}"
    )

    step = step_with_id(CHANGES_JOB, FILTER_STEP_ID)
    uses = step.get("uses")
    assert isinstance(uses, str), (
        f"the {FILTER_STEP_ID!r} step must run an action; found {uses!r}"
    )
    assert uses.startswith(PATHS_FILTER_ACTION), (
        f"the {FILTER_STEP_ID!r} step must run {PATHS_FILTER_ACTION}…; found {uses!r}"
    )


def test_the_changes_job_runs_on_a_github_hosted_runner() -> None:
    """The detector must not run on the runner it exists to avoid paying for."""
    runner = job(CHANGES_JOB).get("runs-on")

    assert runner == "ubuntu-latest", (
        f"the {CHANGES_JOB!r} job must run on ubuntu-latest so that deciding "
        f"whether to spend paid runner minutes costs none; found {runner!r}"
    )


def test_the_benchmark_job_waits_for_the_detector() -> None:
    """`benchmark-ratchet` must declare the `needs` edge its gate reads.

    Without it `needs.changes.outputs.bench` is empty, the gate is false for
    every pull request, and the job silently never benchmarks anything.
    """
    needs = job(BENCHMARK_JOB).get("needs")
    assert isinstance(needs, list), f"the {BENCHMARK_JOB!r} job must declare needs"

    assert CHANGES_JOB in needs, (
        f"the {BENCHMARK_JOB!r} job must list {CHANGES_JOB!r} in `needs`; "
        f"found {needs!r}"
    )


def test_the_benchmark_job_declares_the_expected_gate() -> None:
    """The gate expression must be exactly the rule the rest of these tests model."""
    condition = benchmark_gate()

    assert condition == EXPECTED_GATE, (
        f"the {BENCHMARK_JOB!r} job's `if:` must be {EXPECTED_GATE!r} — pushes to "
        "main always benchmark so the baseline artefact stays fresh, and pull "
        f"requests benchmark only on performance-relevant diffs; found {condition!r}"
    )


def test_a_failed_detector_does_not_benchmark_ungated() -> None:
    """The gate must not name a status function, so a failed `changes` skips.

    `if: always() && (…)` reads as a harmless robustness tweak and is the one
    edit that turns a broken detector into an unconditional paid run.
    """
    named = sorted(fn for fn in STATUS_FUNCTIONS if fn in benchmark_gate())

    assert not named, (
        f"the {BENCHMARK_JOB!r} gate must leave GitHub's implicit `success()` in "
        f"place so a failed {CHANGES_JOB!r} skips the paid job rather than running "
        f"it ungated; found {named}"
    )


def test_a_failed_detector_skips_non_pull_request_events() -> None:
    """A detector failure must skip even a main push's paid benchmark."""
    condition = benchmark_gate()

    assert "needs.changes.result == 'success'" in condition, (
        "the benchmark gate must make detector success explicit so a failed "
        "detector skips all events rather than running ungated"
    )
    assert not benchmark_runs(
        event_name="push", bench=False, detector_succeeded=False
    ), "a failed detector must skip a non-pull-request event"


def test_the_filter_declares_every_performance_relevant_path() -> None:
    """The filter's path list must be exactly the performance-relevant set.

    A missing entry merges a regression unmeasured; a spurious one pays for a
    benchmark that cannot move, which is the cost this gate exists to avoid.
    """
    assert filter_paths() == EXPECTED_FILTER_PATHS, (
        "the `bench` filter must watch exactly the performance-relevant paths; "
        f"missing {sorted(EXPECTED_FILTER_PATHS - filter_paths())}, "
        f"unexpected {sorted(filter_paths() - EXPECTED_FILTER_PATHS)}"
    )


def test_the_filter_uses_only_modelled_pattern_forms() -> None:
    """Every declared pattern must be a form `matches_filter` actually models.

    The property tests below are only evidence about the real filter for as
    long as this holds; a `cuprum/**/*.py` added tomorrow would make the model
    silently over-match rather than fail.
    """
    unmodelled = sorted(
        pattern
        for pattern in filter_paths()
        if not pattern.endswith("/**") and any(c in pattern for c in "*?[")
    )

    assert not unmodelled, (
        "these filter patterns are neither a literal path nor a `dir/**` "
        f"prefix, so `matches_filter` no longer models the filter: {unmodelled}"
    )


def test_the_irrelevant_paths_are_genuinely_irrelevant() -> None:
    """The property tests' docs-only pool must not match the filter."""
    matched = sorted(path for path in IRRELEVANT_PATHS if bench_output([path]))

    assert not matched, (
        "these paths are sampled as performance-irrelevant but the filter "
        f"matches them, so the property tests below prove nothing: {matched}"
    )


def _relevant_paths() -> list[str]:
    """Return one concrete changed path per declared filter pattern."""
    return sorted(
        f"{pattern.removesuffix('**')}pkg/module.rs"
        if pattern.endswith("/**")
        else pattern
        for pattern in filter_paths()
    )


@given(
    relevant=st.lists(st.sampled_from(_relevant_paths()), min_size=1, unique=True),
    irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True),
)
def test_any_performance_relevant_change_benchmarks(
    relevant: list[str], irrelevant: list[str]
) -> None:
    """A pull request touching any watched path benchmarks, however mixed."""
    changed = [*relevant, *irrelevant]

    assert benchmark_runs(
        event_name="pull_request",
        bench=bench_output(changed),
        detector_succeeded=True,
    ), f"a pull request changing {changed} must run {BENCHMARK_JOB!r}"


@given(irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True))
def test_a_pull_request_touching_nothing_relevant_skips(irrelevant: list[str]) -> None:
    """A docs-only pull request — including an empty diff — skips the paid job."""
    assert not benchmark_runs(
        event_name="pull_request",
        bench=bench_output(irrelevant),
        detector_succeeded=True,
    ), f"a pull request changing only {irrelevant} must skip {BENCHMARK_JOB!r}"


@given(
    changed=st.lists(st.sampled_from([*_relevant_paths(), *IRRELEVANT_PATHS])),
    event_name=st.sampled_from(["push", "workflow_dispatch", "schedule"]),
)
def test_a_non_pull_request_event_always_benchmarks(
    changed: list[str], event_name: str
) -> None:
    """Pushes to main benchmark whatever they touch, refreshing the baseline.

    Gating them would make the ratchet compare against an ever-staler
    baseline, which fails open: regressions stop being detected rather than
    being reported.
    """
    assert benchmark_runs(
        event_name=event_name,
        bench=bench_output(changed),
        detector_succeeded=True,
    ), (
        f"a {event_name!r} event changing {changed} must run {BENCHMARK_JOB!r} so "
        "the main baseline artefact is refreshed"
    )


def test_the_gate_decision_is_recorded_in_the_run_summary() -> None:
    """The `changes` job must write its verdict to the run summary.

    A skipped job and a broken gate look identical in the run list, so the
    decision — and the inputs that produced it — has to be stated somewhere a
    maintainer auditing paid-runner spend can read it. What the script
    actually emits for each input is asserted by running it, in
    `tests/behaviour/test_benchmark_gate_summary_behaviour.py`; this only
    pins that the step exists and reads the right things.
    """
    scripts = [
        script
        for step in steps(CHANGES_JOB)
        if isinstance(script := step.get("run"), str)
    ]
    summary_steps = [script for script in scripts if "GITHUB_STEP_SUMMARY" in script]

    assert summary_steps, (
        f"the {CHANGES_JOB!r} job must append its gate decision to $GITHUB_STEP_SUMMARY"
    )

    script = summary_steps[0]
    for operand in ("EVENT", "BENCH", "DETECTOR", "pull_request"):
        assert operand in script, (
            f"the gate summary must read {operand!r} so the recorded decision "
            f"matches the {BENCHMARK_JOB!r} gate; found:\n{script}"
        )


def test_the_gate_decision_is_recorded_even_when_the_detector_fails() -> None:
    """The summary step must not inherit the implicit `success()`.

    A failed detector is the case most worth recording: `benchmark-ratchet`
    then skips for a reason that has nothing to do with the diff. A summary
    that stops being written exactly when the gate misbehaves documents only
    the runs that needed no explanation.
    """
    condition = step_named(CHANGES_JOB, SUMMARY_STEP).get("if")

    assert condition == "${{ !cancelled() }}", (
        f"the {SUMMARY_STEP!r} step must run on every completed run, not only "
        f"when the detector succeeded; found {condition!r}"
    )


def test_the_workflow_serializes_runs_per_ref() -> None:
    """Concurrency must cancel superseded pull-request runs but never main runs.

    Cancelling a `main` run mid-flight abandons the baseline upload, leaving
    later pull requests comparing against a stale commit. Note the narrowness
    of the claim: GitHub replaces a *pending* run when a newer one arrives and
    promises nothing about completion order, so this is not a guarantee that
    baselines are published in commit order. Anything needing that has to
    enforce it where the artefact is written.
    """
    concurrency = mapping(
        typ.cast("dict[str, object]", workflow()).get("concurrency"),
        f"{CI_WORKFLOW} must declare a concurrency policy",
    )

    assert concurrency.get("group") == "ci-${{ github.ref }}", (
        "the concurrency group must be per-ref, so a pull request's runs "
        "supersede each other without touching another ref's; found "
        f"{concurrency.get('group')!r}"
    )
    assert concurrency.get("cancel-in-progress") == (
        "${{ github.event_name == 'pull_request' }}"
    ), (
        "only pull-request runs may be cancelled; a cancelled `main` run never "
        "republishes the baseline artefact. Found "
        f"{concurrency.get('cancel-in-progress')!r}"
    )
