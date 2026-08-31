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
WORKFLOW_DATA = workflow()
FILTER_PATHS = filter_paths(WORKFLOW_DATA)

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
    """Require `changes` to expose the filter verdict as its `bench` output."""
    outputs = mapping(
        job(WORKFLOW_DATA, CHANGES_JOB).get("outputs"),
        f"the {CHANGES_JOB!r} job must declare outputs",
    )

    assert outputs.get(FILTER_NAME) == (
        f"${{{{ steps.{FILTER_STEP_ID}.outputs.{FILTER_NAME} }}}}"
    ), (
        f"the {CHANGES_JOB!r} job must publish its {FILTER_NAME!r} output from the "
        f"{FILTER_STEP_ID!r} step; found {outputs.get(FILTER_NAME)!r}"
    )

    step = step_with_id(WORKFLOW_DATA, CHANGES_JOB, FILTER_STEP_ID)
    uses = step.get("uses")
    assert isinstance(uses, str), (
        f"the {FILTER_STEP_ID!r} step must run an action; found {uses!r}"
    )
    assert uses.startswith(PATHS_FILTER_ACTION), (
        f"the {FILTER_STEP_ID!r} step must run {PATHS_FILTER_ACTION}…; found {uses!r}"
    )


def test_the_changes_job_runs_on_a_github_hosted_runner() -> None:
    """The detector must not run on the runner it exists to avoid paying for."""
    runner = job(WORKFLOW_DATA, CHANGES_JOB).get("runs-on")

    assert runner == "ubuntu-latest", (
        f"the {CHANGES_JOB!r} job must run on ubuntu-latest so that deciding "
        f"whether to spend paid runner minutes costs none; found {runner!r}"
    )


def test_the_detector_runs_on_every_event() -> None:
    """Require detector execution to be independent of the triggering event."""
    changes_job = job(WORKFLOW_DATA, CHANGES_JOB)
    filter_step = step_with_id(WORKFLOW_DATA, CHANGES_JOB, FILTER_STEP_ID)

    assert changes_job.get("if") is None, (
        f"the {CHANGES_JOB!r} job must not filter events before publishing its verdict"
    )
    assert filter_step.get("if") is None, (
        f"the {FILTER_STEP_ID!r} step must run for every event so its output is "
        "always available to the benchmark gate"
    )


def test_the_benchmark_job_waits_for_the_detector() -> None:
    """Require `benchmark-ratchet` to declare the detector dependency."""
    needs = job(WORKFLOW_DATA, BENCHMARK_JOB).get("needs")
    assert isinstance(needs, list), f"the {BENCHMARK_JOB!r} job must declare needs"

    assert CHANGES_JOB in needs, (
        f"the {BENCHMARK_JOB!r} job must list {CHANGES_JOB!r} in `needs`; "
        f"found {needs!r}"
    )


def test_the_benchmark_job_declares_the_expected_gate() -> None:
    """Require the gate expression to match the model used by these tests."""
    condition = benchmark_gate(WORKFLOW_DATA)

    assert condition == EXPECTED_GATE, (
        f"the {BENCHMARK_JOB!r} job's `if:` must be {EXPECTED_GATE!r} — pushes to "
        "main always benchmark so the baseline artefact stays fresh, and pull "
        f"requests benchmark only on performance-relevant diffs; found {condition!r}"
    )


def test_a_failed_detector_does_not_benchmark_ungated() -> None:
    """Require a failed detector to skip the paid benchmark rather than run ungated."""
    named = sorted(fn for fn in STATUS_FUNCTIONS if fn in benchmark_gate(WORKFLOW_DATA))

    assert not named, (
        f"the {BENCHMARK_JOB!r} gate must leave GitHub's implicit `success()` in "
        f"place so a failed {CHANGES_JOB!r} skips the paid job rather than running "
        f"it ungated; found {named}"
    )


def test_a_failed_detector_skips_non_pull_request_events() -> None:
    """Require detector failure to skip the paid benchmark for every event."""
    condition = benchmark_gate(WORKFLOW_DATA)

    assert "needs.changes.result == 'success'" in condition, (
        "the benchmark gate must make detector success explicit so a failed "
        "detector skips all events rather than running ungated"
    )
    assert not benchmark_runs(
        event_name="push", bench=False, detector_succeeded=False
    ), "a failed detector must skip a non-pull-request event"


def test_the_filter_declares_every_performance_relevant_path() -> None:
    """Require the filter path list to be exactly the performance-relevant set."""
    assert FILTER_PATHS == EXPECTED_FILTER_PATHS, (
        "the `bench` filter must watch exactly the performance-relevant paths; "
        f"missing {sorted(EXPECTED_FILTER_PATHS - FILTER_PATHS)}, "
        f"unexpected {sorted(FILTER_PATHS - EXPECTED_FILTER_PATHS)}"
    )


def test_the_filter_uses_only_modelled_pattern_forms() -> None:
    """Require every declared filter pattern to use a modelled form."""
    unmodelled = sorted(
        pattern
        for pattern in FILTER_PATHS
        if not pattern.endswith("/**") and any(c in pattern for c in "*?[")
    )

    assert not unmodelled, (
        "these filter patterns are neither a literal path nor a `dir/**` "
        f"prefix, so `matches_filter` no longer models the filter: {unmodelled}"
    )


def test_the_irrelevant_paths_are_genuinely_irrelevant() -> None:
    """Require the property tests' docs-only paths to remain outside the filter."""
    matched = sorted(
        path for path in IRRELEVANT_PATHS if bench_output([path], FILTER_PATHS)
    )

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
        for pattern in FILTER_PATHS
    )


@given(
    relevant=st.lists(st.sampled_from(_relevant_paths()), min_size=1, unique=True),
    irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True),
)
def test_any_performance_relevant_change_benchmarks(
    relevant: list[str], irrelevant: list[str]
) -> None:
    """Require a benchmark when any watched path changes.

    Parameters
    ----------
    relevant : list[str]
        Performance-relevant paths included in the sampled pull request.
    irrelevant : list[str]
        Non-performance paths mixed into the sampled pull request.
    """
    changed = [*relevant, *irrelevant]

    assert benchmark_runs(
        event_name="pull_request",
        bench=bench_output(changed, FILTER_PATHS),
        detector_succeeded=True,
    ), f"a pull request changing {changed} must run {BENCHMARK_JOB!r}"


@given(irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True))
def test_a_pull_request_touching_nothing_relevant_skips(irrelevant: list[str]) -> None:
    """Require a skip when a pull request touches no watched paths.

    Parameters
    ----------
    irrelevant : list[str]
        Non-performance paths included in the sampled pull request.
    """
    assert not benchmark_runs(
        event_name="pull_request",
        bench=bench_output(irrelevant, FILTER_PATHS),
        detector_succeeded=True,
    ), f"a pull request changing only {irrelevant} must skip {BENCHMARK_JOB!r}"


@given(
    changed=st.lists(st.sampled_from([*_relevant_paths(), *IRRELEVANT_PATHS])),
    event_name=st.sampled_from(["push", "workflow_dispatch", "schedule"]),
)
def test_a_non_pull_request_event_always_benchmarks(
    changed: list[str], event_name: str
) -> None:
    """Require benchmarking for every non-pull-request event.

    Parameters
    ----------
    changed : list[str]
        Paths included in the sampled event.
    event_name : str
        Non-pull-request event type used by the sampled case.

    """
    assert benchmark_runs(
        event_name=event_name,
        bench=bench_output(changed, FILTER_PATHS),
        detector_succeeded=True,
    ), (
        f"a {event_name!r} event changing {changed} must run {BENCHMARK_JOB!r} so "
        "the main baseline artefact is refreshed"
    )


def test_the_gate_decision_is_recorded_in_the_run_summary() -> None:
    """Require the `changes` job to record its verdict in the run summary."""
    scripts = [
        script
        for step in steps(WORKFLOW_DATA, CHANGES_JOB)
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
    """Require the summary step to record decisions when detector execution fails."""
    condition = step_named(WORKFLOW_DATA, CHANGES_JOB, SUMMARY_STEP).get("if")

    assert condition == "${{ !cancelled() }}", (
        f"the {SUMMARY_STEP!r} step must run on every completed run, not only "
        f"when the detector succeeded; found {condition!r}"
    )


def test_the_workflow_serializes_runs_per_ref() -> None:
    """Require per-ref concurrency that cancels pull-request runs only."""
    concurrency = mapping(
        typ.cast("dict[str, object]", WORKFLOW_DATA).get("concurrency"),
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
