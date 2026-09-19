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
  whether to spend it;
- state the ratchet's thresholds differently from the module that owns them,
  and the job applies numbers nobody chose — a wider floor hides real
  regressions, a narrower one restores the false positives of issue #219;
- gate the sample-recording or baseline-upload step on the ratchet's verdict,
  and a main run that measured a slowdown stops publishing the sample that
  would have corrected the window (issue #219).

No ordinary test notices any of that, so these tests parse `ci.yml` and read
the contract back. They pin the *declarations*; the decision those
declarations produce for a given pull request is stated in
`tests/behaviour/test_benchmark_path_gate_behaviour.py`, and both suites read
the workflow through `tests.helpers.workflow`. The build half of the same
workflow's contract — which job builds the extension, and how — lives in
`test_extension_ci_contract.py`.
"""

from __future__ import annotations

import re
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from benchmarks.pipeline_throughput_scenarios import CI_RATCHET_WORKER_ITERATIONS
from benchmarks.ratchet_history import (
    DEFAULT_MAX_REGRESSION,
    DEFAULT_NOISE_SIGMAS,
    DEFAULT_WINDOW_SIZE,
)
from tests.helpers.workflow import (
    BENCHMARK_JOB,
    CHANGES_JOB,
    CI_WORKFLOW,
    FILTER_NAME,
    FILTER_STEP_ID,
    Workflow,
    bench_output,
    benchmark_gate,
    benchmark_runs,
    first_step_running,
    job,
    mapping,
    parse_workflow,
    script_of,
    step_named,
    step_with_id,
    steps,
)
from tests.helpers.workflow_shell import (
    flag_value,
    shell_function,
    shell_statements,
    top_level_operators,
)

PATHS_FILTER_ACTION = "dorny/paths-filter@"
SUMMARY_STEP = "Record the benchmark gate decision"
CHECKOUT_STEP = "Check out repository"
THROUGHPUT_STEP = "Run throughput benchmarks and ratchet comparison"
SAMPLE_STEP = "Record this run's benchmark sample"
BASELINE_UPLOAD_STEP = "Upload main benchmark baseline artifact"
#: The shell function in `THROUGHPUT_STEP` that runs the ratchet comparison.
RATCHET_FUNCTION = "run_ratchet"
#: The shell function in `THROUGHPUT_STEP` that measures, as distinct from the
#: one that judges. The measurement flags live here.
RATCHET_BENCHMARKS_FUNCTION = "run_ratchet_benchmarks"
#: The only step state the publication steps may read: whether this run produced
#: candidate artefacts at all. Reading anything else — the ratchet's outcome in
#: particular — is what would let a failing run withhold its own sample.
ARTEFACT_STEP = "candidate-artefacts"
ARTEFACT_AVAILABLE = f"steps.{ARTEFACT_STEP}.outputs.available == 'true'"
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


def test_the_changes_job_publishes_the_filter_result(workflow_data: Workflow) -> None:
    """Require `changes` to expose the filter verdict as its `bench` output."""
    outputs = mapping(
        job(workflow_data, CHANGES_JOB).get("outputs"),
        f"the {CHANGES_JOB!r} job must declare outputs",
    )

    assert outputs.get(FILTER_NAME) == (
        f"${{{{ steps.{FILTER_STEP_ID}.outputs.{FILTER_NAME} }}}}"
    ), (
        f"the {CHANGES_JOB!r} job must publish its {FILTER_NAME!r} output from the "
        f"{FILTER_STEP_ID!r} step; found {outputs.get(FILTER_NAME)!r}"
    )

    step = step_with_id(workflow_data, CHANGES_JOB, FILTER_STEP_ID)
    uses = step.get("uses")
    assert isinstance(uses, str), (
        f"the {FILTER_STEP_ID!r} step must run an action; found {uses!r}"
    )
    assert uses.startswith(PATHS_FILTER_ACTION), (
        f"the {FILTER_STEP_ID!r} step must run {PATHS_FILTER_ACTION}…; found {uses!r}"
    )


def test_the_changes_job_runs_on_the_lane_it_gates(
    workflow_data: Workflow,
) -> None:
    """The detector runs where the required check waiting on it runs.

    It used to run GitHub-hosted so that deciding whether to spend paid
    minutes cost none. That saved seconds and cost a pull request 46 minutes
    on 2026-09-23 (run 35904789287), when the hosted queue held it while every
    Ubicloud job started. A fork's pull request takes the hosted arm, because
    a fork cannot obtain an Ubicloud runner.
    """
    runner = " ".join(str(job(workflow_data, CHANGES_JOB).get("runs-on")).split())
    expected = (
        "${{ github.event.pull_request.head.repo.fork "
        "&& 'ubuntu-latest' || 'ubicloud-standard-2' }}"
    )

    assert runner == expected, (
        f"the {CHANGES_JOB!r} job must run on the Ubicloud lane with the fork "
        f"fallback, {expected!r}; found {runner!r}"
    )


def test_the_changes_job_has_only_the_permissions_its_filter_needs(
    workflow_data: Workflow,
) -> None:
    """Paths-filter needs changed-file read access without write authority."""
    permissions = mapping(
        job(workflow_data, CHANGES_JOB).get("permissions"),
        f"the {CHANGES_JOB!r} job must declare narrow permissions",
    )

    assert permissions == {"contents": "read", "pull-requests": "read"}, (
        f"the {CHANGES_JOB!r} job must retain only the filter's read permissions; "
        f"found {permissions!r}"
    )


def test_the_changes_checkout_does_not_persist_credentials(
    workflow_data: Workflow,
) -> None:
    """The cheap detector must not write its token into Git configuration."""
    checkout = step_named(workflow_data, CHANGES_JOB, CHECKOUT_STEP)
    checkout_options = mapping(
        checkout.get("with"),
        f"the {CHANGES_JOB!r} checkout must declare explicit options",
    )

    assert checkout_options.get("persist-credentials") is False, (
        f"the {CHANGES_JOB!r} checkout must disable credential persistence; "
        f"found {checkout_options.get('persist-credentials')!r}"
    )


def test_the_paid_benchmark_checkout_does_not_persist_credentials(
    workflow_data: Workflow,
) -> None:
    """The paid job fetches its baseline explicitly instead of retaining a token."""
    checkout = step_named(workflow_data, BENCHMARK_JOB, CHECKOUT_STEP)
    checkout_options = mapping(
        checkout.get("with"),
        f"the {BENCHMARK_JOB!r} checkout must declare explicit options",
    )

    assert checkout_options.get("persist-credentials") is False, (
        f"the {BENCHMARK_JOB!r} checkout must disable credential persistence; "
        f"found {checkout_options.get('persist-credentials')!r}"
    )


def test_the_paid_benchmark_uses_the_shared_optimized_setup(
    workflow_data: Workflow,
) -> None:
    """The paid runner builds once through the local contributor target."""
    script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
    assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"

    assert "make develop MATURIN_DEVELOP_FLAGS='--release --skip-install'" in script, (
        "the benchmark must use the shared optimized extension-build target"
    )
    assert "UV_CACHE_DIR=.uv-cache UV_TOOL_DIR=.uv-tools uv run python" in script, (
        "the benchmark scripts must reuse checkout-local uv caches and tools"
    )


def test_the_ratchet_policy_matches_the_module_defaults(
    workflow_data: Workflow,
) -> None:
    """Require the workflow's ratchet thresholds to be the module's own.

    `benchmarks/ratchet_history.py` owns the policy — the flat floor, the noise
    multiplier, and the window size — and the ratchet CLI defaults to those same
    values. The workflow restates all three anyway, so that the job reads as the
    policy it applies instead of inheriting whatever the module currently says.
    That restatement is only safe while something notices when the two drift:
    otherwise a change to the module leaves the job silently applying the old
    numbers, and the failure is expensive in both directions. A wider floor
    hides real regressions; a narrower one reinstates the false positives of
    issue #219.
    """
    script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
    assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
    body = shell_function(script, RATCHET_FUNCTION, step=THROUGHPUT_STEP)

    max_regression = float(flag_value(body, "--max-regression"))
    assert max_regression == DEFAULT_MAX_REGRESSION, (
        f"--max-regression must be the {DEFAULT_MAX_REGRESSION!r} that "
        f"benchmarks/ratchet_history.py owns; found {max_regression!r}"
    )

    noise_sigmas = float(flag_value(body, "--noise-sigmas"))
    assert noise_sigmas == DEFAULT_NOISE_SIGMAS, (
        f"--noise-sigmas must be the {DEFAULT_NOISE_SIGMAS!r} that "
        f"benchmarks/ratchet_history.py owns; found {noise_sigmas!r}"
    )

    window_size = int(flag_value(body, "--history-window"))
    assert window_size == DEFAULT_WINDOW_SIZE, (
        f"--history-window must be the {DEFAULT_WINDOW_SIZE!r} that "
        f"benchmarks/ratchet_history.py owns; found {window_size!r}"
    )


def test_the_ratchet_benchmarks_measure_the_ci_ratchet_workload(
    workflow_data: Workflow,
) -> None:
    """Require the gate to measure the `--ci-ratchet` workload, not a smoke run.

    `run_ratchet_benchmarks` is the whole of what the gate measures, so the
    workload flag it passes is the gate's definition rather than a detail of
    it. `--smoke` is the tempting alternative — it exercises the same shape at
    smaller payloads and finishes sooner — but it measures a different payload
    than the one the recorded samples were taken at, and the ratchet only
    compares samples whose profile metadata agrees. Selecting the wrong one
    would leave every candidate incomparable with the window rather than
    visibly wrong.

    The whole-body check is what makes this more than a flag-presence test:
    `--ci-ratchet` and `--smoke` are mutually exclusive, so a body carrying
    both is not a valid invocation however the flags are spelled.
    """
    script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
    assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
    body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

    assert "--ci-ratchet" in body, (
        f"{RATCHET_BENCHMARKS_FUNCTION!r} must measure the --ci-ratchet "
        f"workload; found: {body}"
    )
    assert "--smoke" not in body, (
        f"{RATCHET_BENCHMARKS_FUNCTION!r} must not pass --smoke, which selects "
        "a different payload than the recorded samples were measured at; "
        f"found: {body}"
    )


def test_the_ratchet_benchmarks_invocation_propagates_failure(
    workflow_data: Workflow,
) -> None:
    """Require each measured command to abort the function when it fails.

    The confirmation path invokes this function as a condition operand, and
    Bash suspends `errexit` for a function body invoked that way. Without an
    explicit guard on each command, a failed `make develop` or a failed
    benchmark would fall through to the next command and the function would
    return the last command's status — so the confirmation branch would read a
    stale or missing plan as a measured result rather than as a failure. An
    explicit `set -e` inside the body does not restore aborting, which is why
    this pins the guard rather than trusting the shell option.
    """
    script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
    assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
    body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

    statements = shell_statements(body)
    for marker in (
        "make develop MATURIN_DEVELOP_FLAGS",
        "benchmarks/pipeline_throughput.py",
        "benchmarks/ci_benchmark_ratchet_profile.py",
    ):
        matching = [stmt for stmt in statements if marker in stmt]
        assert matching, (
            f"{marker!r} must be invoked by {RATCHET_BENCHMARKS_FUNCTION!r}"
        )
        for statement in matching:
            assert statement.endswith("|| return $?"), (
                f"the {marker!r} invocation in {RATCHET_BENCHMARKS_FUNCTION!r} "
                "must guard itself with `|| return $?`, so its failure reaches "
                "the caller instead of being masked by the next command: "
                f"{statement}"
            )


def test_the_ratchet_worker_iterations_match_the_scenario_default(
    workflow_data: Workflow,
) -> None:
    """Require the workflow's iteration count to be the scenario module's own.

    The count is measurement protocol, not a tuning dial: it is recorded in
    every sample and the ratchet only compares samples whose profile metadata
    agrees, so a workflow that measured at one count while the module's
    default named another would silently make every local reproduction
    incomparable rather than visibly wrong. `--worker-iterations` therefore
    appears in two places that must agree — the workflow's flag, and the
    default the CLI resolves for `--ci-ratchet` — and this asserts they do.
    """
    script = script_of(step_named(workflow_data, BENCHMARK_JOB, THROUGHPUT_STEP))
    assert script is not None, f"the {THROUGHPUT_STEP!r} step must run a script"
    body = shell_function(script, RATCHET_BENCHMARKS_FUNCTION, step=THROUGHPUT_STEP)

    iterations = int(flag_value(body, "--worker-iterations"))
    assert iterations == CI_RATCHET_WORKER_ITERATIONS, (
        f"--worker-iterations must be the {CI_RATCHET_WORKER_ITERATIONS!r} "
        f"that benchmarks/pipeline_throughput_scenarios.py owns; "
        f"found {iterations!r}"
    )


@pytest.mark.parametrize(
    "step_name",
    [
        pytest.param(SAMPLE_STEP, id="record-sample"),
        pytest.param(BASELINE_UPLOAD_STEP, id="upload-baseline"),
    ],
)
def test_the_main_sample_is_published_whatever_the_ratchet_decides(
    workflow_data: Workflow,
    step_name: str,
) -> None:
    """Require publication to depend on the measurement, never on the verdict.

    Both steps publish what a `main` run measured. A window fed only by passing
    runs is a window of low-biased samples: a measurement faster than the bar is
    always accepted, while the slower measurements that would correct it are the
    ones a failing run would withhold. So the conditions must run on every
    completed run — `!cancelled()` rather than GitHub's implicit `success()` —
    and must read only whether candidate artefacts exist, never the ratchet's
    outcome.
    """
    condition = step_named(workflow_data, BENCHMARK_JOB, step_name).get("if")
    assert isinstance(condition, str), (
        f"the {step_name!r} step must declare an `if:` condition; found {condition!r}"
    )

    assert "!cancelled()" in condition, (
        f"the {step_name!r} step must run after a failed ratchet as well as a "
        "passed one, so its condition needs `!cancelled()`; an interrupted run "
        f"that measured half a sample still publishes nothing. Found: {condition!r}"
    )
    assert top_level_operators(condition, "||") == 0, (
        f"the {step_name!r} step must require all of its guards, not any of "
        "them: a top-level disjunction would let publication proceed with no "
        f"candidate artefacts. Found: {condition!r}"
    )
    assert top_level_operators(condition, "&&") >= 2, (
        f"the {step_name!r} step must combine its guards conjunctively, so "
        "that every one of them has to hold for the step to run. "
        f"Found: {condition!r}"
    )
    assert ARTEFACT_AVAILABLE in condition, (
        f"the {step_name!r} step must still require this run to have produced "
        f"candidate artefacts. Found: {condition!r}"
    )

    referenced = set(re.findall(r"steps\.([A-Za-z0-9_-]+)\.", condition))
    assert referenced == {ARTEFACT_STEP}, (
        f"the {step_name!r} step may read only {ARTEFACT_STEP!r} step state; found "
        f"{sorted(referenced)}. Reading the ratchet's outcome here would withhold "
        "the sample of exactly the runs the window most needs, which is how the "
        f"baseline became low-biased (issue #219). Found: {condition!r}"
    )


def test_the_detector_runs_on_every_event(workflow_data: Workflow) -> None:
    """Require detector execution to be independent of the triggering event."""
    changes_job = job(workflow_data, CHANGES_JOB)
    filter_step = step_with_id(workflow_data, CHANGES_JOB, FILTER_STEP_ID)

    assert changes_job.get("if") is None, (
        f"the {CHANGES_JOB!r} job must not filter events before publishing its verdict"
    )
    assert filter_step.get("if") is None, (
        f"the {FILTER_STEP_ID!r} step must run for every event so its output is "
        "always available to the benchmark gate"
    )


def test_the_benchmark_job_waits_for_the_detector(workflow_data: Workflow) -> None:
    """Require `benchmark-ratchet` to declare the detector dependency."""
    needs = job(workflow_data, BENCHMARK_JOB).get("needs")
    assert isinstance(needs, list), f"the {BENCHMARK_JOB!r} job must declare needs"

    assert CHANGES_JOB in needs, (
        f"the {BENCHMARK_JOB!r} job must list {CHANGES_JOB!r} in `needs`; "
        f"found {needs!r}"
    )


def test_the_benchmark_job_declares_the_expected_gate(
    workflow_data: Workflow,
) -> None:
    """Require the gate expression to match the model used by these tests."""
    condition = benchmark_gate(workflow_data)

    assert condition == EXPECTED_GATE, (
        f"the {BENCHMARK_JOB!r} job's `if:` must be {EXPECTED_GATE!r} — pushes to "
        "main always benchmark so the baseline artefact stays fresh, and pull "
        f"requests benchmark only on performance-relevant diffs; found {condition!r}"
    )


def test_a_failed_detector_does_not_benchmark_ungated(
    workflow_data: Workflow,
) -> None:
    """Require a failed detector to skip the paid benchmark rather than run ungated."""
    named = sorted(fn for fn in STATUS_FUNCTIONS if fn in benchmark_gate(workflow_data))

    assert not named, (
        f"the {BENCHMARK_JOB!r} gate must leave GitHub's implicit `success()` in "
        f"place so a failed {CHANGES_JOB!r} skips the paid job rather than running "
        f"it ungated; found {named}"
    )


def test_a_failed_detector_skips_non_pull_request_events(
    workflow_data: Workflow,
) -> None:
    """Require detector failure to skip the paid benchmark for every event."""
    condition = benchmark_gate(workflow_data)

    assert "needs.changes.result == 'success'" in condition, (
        "the benchmark gate must make detector success explicit so a failed "
        "detector skips all events rather than running ungated"
    )
    assert not benchmark_runs(
        event_name="push", bench=False, detector_succeeded=False
    ), "a failed detector must skip a non-pull-request event"


def test_the_filter_declares_every_performance_relevant_path(
    filter_path_patterns: frozenset[str],
) -> None:
    """Require the filter path list to be exactly the performance-relevant set."""
    assert filter_path_patterns == EXPECTED_FILTER_PATHS, (
        "the `bench` filter must watch exactly the performance-relevant paths; "
        f"missing {sorted(EXPECTED_FILTER_PATHS - filter_path_patterns)}, "
        f"unexpected {sorted(filter_path_patterns - EXPECTED_FILTER_PATHS)}"
    )


def test_the_filter_uses_only_modelled_pattern_forms(
    filter_path_patterns: frozenset[str],
) -> None:
    """Require every declared filter pattern to use a modelled form."""
    unmodelled = sorted(
        pattern
        for pattern in filter_path_patterns
        if not pattern.endswith("/**") and any(c in pattern for c in "*?[")
    )

    assert not unmodelled, (
        "these filter patterns are neither a literal path nor a `dir/**` "
        f"prefix, so `matches_filter` no longer models the filter: {unmodelled}"
    )


def test_the_irrelevant_paths_are_genuinely_irrelevant(
    filter_path_patterns: frozenset[str],
) -> None:
    """Require the property tests' docs-only paths to remain outside the filter."""
    matched = sorted(
        path for path in IRRELEVANT_PATHS if bench_output([path], filter_path_patterns)
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
        for pattern in EXPECTED_FILTER_PATHS
    )


@given(
    relevant=st.lists(st.sampled_from(_relevant_paths()), min_size=1, unique=True),
    irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True),
)
def test_any_performance_relevant_change_benchmarks(
    relevant: list[str],
    irrelevant: list[str],
    filter_path_patterns: frozenset[str],
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
        bench=bench_output(changed, filter_path_patterns),
        detector_succeeded=True,
    ), f"a pull request changing {changed} must run {BENCHMARK_JOB!r}"


@given(irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True))
def test_a_pull_request_touching_nothing_relevant_skips(
    irrelevant: list[str], filter_path_patterns: frozenset[str]
) -> None:
    """Require a skip when a pull request touches no watched paths.

    Parameters
    ----------
    irrelevant : list[str]
        Non-performance paths included in the sampled pull request.
    """
    assert not benchmark_runs(
        event_name="pull_request",
        bench=bench_output(irrelevant, filter_path_patterns),
        detector_succeeded=True,
    ), f"a pull request changing only {irrelevant} must skip {BENCHMARK_JOB!r}"


@given(
    changed=st.lists(st.sampled_from([*_relevant_paths(), *IRRELEVANT_PATHS])),
    event_name=st.sampled_from(["push", "workflow_dispatch", "schedule"]),
)
def test_a_non_pull_request_event_always_benchmarks(
    changed: list[str], event_name: str, filter_path_patterns: frozenset[str]
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
        bench=bench_output(changed, filter_path_patterns),
        detector_succeeded=True,
    ), (
        f"a {event_name!r} event changing {changed} must run {BENCHMARK_JOB!r} so "
        "the main baseline artefact is refreshed"
    )


def test_the_gate_decision_is_recorded_in_the_run_summary(
    workflow_data: Workflow,
) -> None:
    """Require the `changes` job to record its verdict in the run summary."""
    changes_steps = steps(workflow_data, CHANGES_JOB)
    filter_index = next(
        index
        for index, step in enumerate(changes_steps)
        if step.get("id") == FILTER_STEP_ID
    )
    summary_step = step_named(workflow_data, CHANGES_JOB, SUMMARY_STEP)
    summary_index = changes_steps.index(summary_step)
    script = script_of(summary_step)
    assert script is not None, f"the {SUMMARY_STEP!r} step must run a script"

    assert filter_index < summary_index, (
        f"the {FILTER_STEP_ID!r} step must precede {SUMMARY_STEP!r} so the summary "
        "records the detector's actual outcome and output"
    )

    for operand in ("EVENT", "BENCH", "DETECTOR", "pull_request"):
        assert operand in script, (
            f"the gate summary must read {operand!r} so the recorded decision "
            f"matches the {BENCHMARK_JOB!r} gate; found:\n{script}"
        )


def test_the_gate_decision_is_recorded_even_when_the_detector_fails(
    workflow_data: Workflow,
) -> None:
    """Require the summary step to record decisions when detector execution fails."""
    condition = step_named(workflow_data, CHANGES_JOB, SUMMARY_STEP).get("if")

    assert condition == "${{ !cancelled() }}", (
        f"the {SUMMARY_STEP!r} step must run on every completed run, not only "
        f"when the detector succeeded; found {condition!r}"
    )


def test_the_workflow_serializes_runs_per_ref(workflow_data: Workflow) -> None:
    """Require per-ref concurrency that cancels pull-request runs only."""
    concurrency = mapping(
        typ.cast("dict[str, object]", workflow_data).get("concurrency"),
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


@pytest.mark.parametrize(
    ("newer_event", "expected_cancellation"),
    [
        pytest.param("pull_request", True, id="superseding-pull-request"),
        pytest.param("push", False, id="superseding-main-push"),
        pytest.param("workflow_dispatch", False, id="superseding-manual-run"),
    ],
)
def test_a_new_run_cancels_only_superseded_pull_request_runs(
    workflow_data: Workflow,
    newer_event: str,
    expected_cancellation: bool,
) -> None:
    """Model the cancellation policy for a newer run on the same ref.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow whose concurrency policy is modelled.
    newer_event : str
        Event that starts the newer run in the ref's concurrency group.
    expected_cancellation : bool
        Whether that newer run must cancel an in-progress predecessor.
    """
    concurrency = mapping(
        typ.cast("dict[str, object]", workflow_data).get("concurrency"),
        f"{CI_WORKFLOW} must declare a concurrency policy",
    )
    condition = concurrency.get("cancel-in-progress")

    assert condition == "${{ github.event_name == 'pull_request' }}"
    assert (newer_event == "pull_request") is expected_cancellation


def test_mapping_rejects_non_string_keys() -> None:
    """Reject YAML mappings whose keys cannot satisfy the helper's model."""
    with pytest.raises(AssertionError, match="string-keyed mapping"):
        mapping({"jobs": {}, 1: "unexpected"}, "string-keyed mapping")


def test_parse_workflow_rejects_a_numeric_top_level_key() -> None:
    """Reject a numeric key rather than treating it as GitHub Actions ``on``."""
    with pytest.raises(AssertionError, match="must parse to a mapping"):
        parse_workflow("1: unexpected\n")


def test_step_lookups_return_matches_and_explain_missing_steps() -> None:
    """Look up declared steps by id and name without changing diagnostics."""
    workflow_data = parse_workflow(
        """
        jobs:
          changes:
            steps:
              - id: filter
                name: Detect relevant changes
              - id: summary
                name: Record the decision
        """
    )

    assert step_with_id(workflow_data, "changes", "filter") == {
        "id": "filter",
        "name": "Detect relevant changes",
    }
    assert step_named(workflow_data, "changes", "Record the decision") == {
        "id": "summary",
        "name": "Record the decision",
    }
    with pytest.raises(AssertionError, match="step with id 'missing'"):
        step_with_id(workflow_data, "changes", "missing")
    with pytest.raises(
        AssertionError,
        match=r"found \['Detect relevant changes', 'Record the decision'\]",
    ):
        step_named(workflow_data, "changes", "missing")


@pytest.mark.parametrize(
    "invalid_step",
    [
        pytest.param("not a mapping", id="scalar"),
        pytest.param({1: "non-string key"}, id="non-string-key"),
    ],
)
def test_steps_reject_non_mapping_entries(invalid_step: object) -> None:
    """Reject a declared step that cannot satisfy the narrow mapping model."""
    workflow_data = typ.cast(
        "Workflow",
        {"jobs": {CHANGES_JOB: {"steps": [invalid_step]}}},
    )

    with pytest.raises(AssertionError, match="must declare mapping steps"):
        steps(workflow_data, CHANGES_JOB)


def test_first_step_running_propagates_malformed_shell_quoting() -> None:
    """Expose malformed workflow shell syntax instead of treating it as absent."""
    workflow_data = parse_workflow(
        """
        jobs:
          changes:
            steps:
              - run: "make 'develop"
        """
    )

    with pytest.raises(ValueError, match="No closing quotation"):
        first_step_running(workflow_data, "make develop", job_name=CHANGES_JOB)
