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
  stop refreshing the baseline artifact that pull-request runs compare
  against, so the ratchet quietly degrades to comparing against nothing;
- move `changes` onto the paid runner and the saving is spent detecting
  whether to spend it.

No ordinary test notices any of that, so these tests parse `ci.yml` and read
the contract back. The build half of the same workflow's contract — which job
builds the extension, and how — lives in `test_extension_ci_contract.py`.

`yaml.safe_load` returns `typing.Any`, which erases every mistake an assertion
can make about the shape it reads, so the shapes below declare the keys these
tests reach for. Their *values* stay `object` and are narrowed where read.
"""

from __future__ import annotations

import functools
import typing as typ

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st

from tests.helpers.docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CI_WORKFLOW = ".github/workflows/ci.yml"

CHANGES_JOB = "changes"
BENCHMARK_JOB = "benchmark-ratchet"
FILTER_STEP_ID = "filter"
FILTER_NAME = "bench"
PATHS_FILTER_ACTION = "dorny/paths-filter@"

#: The gate, verbatim. Pinning the whole expression rather than probing it for
#: substrings is what makes an inverted or half-deleted condition a failure:
#: `needs.changes.outputs.bench != 'true'` contains every operand the loose
#: check would look for.
EXPECTED_GATE = (
    "github.event_name != 'pull_request' || needs.changes.outputs.bench == 'true'"
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
#: holds the pool to that, so the property test below cannot pass by
#: accidentally sampling a performance-relevant path.
IRRELEVANT_PATHS = (
    "README.md",
    "CHANGELOG.md",
    "docs/users-guide.md",
    "docs/execplans/4-4-3-ratchet-rust-performance.md",
    ".github/workflows/release.yml",
    ".github/actionlint.yaml",
)


class Step(typ.TypedDict, total=False):
    """One step of a job, declaring only the keys these tests read."""

    id: object
    uses: object
    run: object


class Job(typ.TypedDict, total=False):
    """One job of a workflow, declaring only the keys these tests read."""

    needs: object
    outputs: object
    steps: list[Step]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow file, declaring only the keys these tests read."""

    concurrency: object
    jobs: dict[str, Job]


@functools.cache
def _workflow() -> Workflow:
    """Parse the CI workflow."""
    parsed = yaml.safe_load((repo_root() / CI_WORKFLOW).read_text(encoding="utf-8"))
    assert isinstance(parsed, dict), f"{CI_WORKFLOW} must parse to a mapping"
    return typ.cast("Workflow", parsed)


def _job(job_name: str) -> dict[str, object]:
    """Return a named job, failing with the available names when absent."""
    jobs = _workflow().get("jobs")
    assert isinstance(jobs, dict), f"{CI_WORKFLOW} must declare a jobs mapping"
    job = jobs.get(job_name)
    assert isinstance(job, dict), (
        f"{CI_WORKFLOW} must declare a {job_name!r} job; found {sorted(jobs)}"
    )
    return typ.cast("dict[str, object]", job)


def _steps(job_name: str) -> list[dict[str, object]]:
    """Return the steps of a named job."""
    steps = _job(job_name).get("steps")
    assert isinstance(steps, list), f"the {job_name!r} job must declare steps"
    return typ.cast("list[dict[str, object]]", steps)


def _step_with_id(job_name: str, step_id: str) -> dict[str, object]:
    """Return the step of a job carrying a given `id:`."""
    for step in _steps(job_name):
        if step.get("id") == step_id:
            return step
    pytest.fail(f"the {job_name!r} job must declare a step with id {step_id!r}")


def _mapping(value: object, message: str) -> dict[str, object]:
    """Assert that a value read from the workflow is a mapping, and type it.

    `yaml.safe_load` produces mappings of unknown key type, which makes every
    subsequent `.get("…")` a type error rather than a narrowing.
    """
    assert isinstance(value, dict), message
    return typ.cast("dict[str, object]", value)


@functools.cache
def _filter_paths() -> frozenset[str]:
    """Return the path patterns the `bench` filter declares."""
    step = _step_with_id(CHANGES_JOB, FILTER_STEP_ID)
    inputs = _mapping(
        step.get("with"),
        f"the {FILTER_STEP_ID!r} step must pass inputs to the filter action",
    )
    filters = _mapping(
        yaml.safe_load(str(inputs["filters"])),
        "the `filters` input must parse to a mapping",
    )
    patterns = filters.get(FILTER_NAME)
    assert isinstance(patterns, list), (
        f"the filter must declare a {FILTER_NAME!r} list; found {sorted(filters)}"
    )
    return frozenset(str(pattern) for pattern in patterns)


def _matches(pattern: str, path: str) -> bool:
    """Return whether a changed `path` matches a declared filter `pattern`.

    A bounded model of the two pattern forms the filter is allowed to use;
    `test_the_filter_uses_only_modelled_pattern_forms` keeps it honest.
    """
    if pattern.endswith("/**"):
        return path.startswith(pattern.removesuffix("**"))
    return path == pattern


def _bench_output(changed_paths: cabc.Collection[str]) -> bool:
    """Model the `bench` output the filter produces for a set of changes."""
    return any(
        _matches(pattern, path) for pattern in _filter_paths() for path in changed_paths
    )


def _benchmark_runs(*, event_name: str, bench: bool) -> bool:
    """Model `EXPECTED_GATE`, returning whether `benchmark-ratchet` runs."""
    return event_name != "pull_request" or bench


def test_the_changes_job_publishes_the_filter_result() -> None:
    """`changes` must expose the filter's verdict as its `bench` output.

    The output is the whole interface between the two jobs; without it the
    gate reads an empty string and every pull request skips the benchmark.
    """
    outputs = _mapping(
        _job(CHANGES_JOB).get("outputs"),
        f"the {CHANGES_JOB!r} job must declare outputs",
    )

    assert outputs.get(FILTER_NAME) == (
        f"${{{{ steps.{FILTER_STEP_ID}.outputs.{FILTER_NAME} }}}}"
    ), (
        f"the {CHANGES_JOB!r} job must publish its {FILTER_NAME!r} output from the "
        f"{FILTER_STEP_ID!r} step; found {outputs.get(FILTER_NAME)!r}"
    )

    step = _step_with_id(CHANGES_JOB, FILTER_STEP_ID)
    uses = step.get("uses")
    assert isinstance(uses, str), (
        f"the {FILTER_STEP_ID!r} step must run an action; found {uses!r}"
    )
    assert uses.startswith(PATHS_FILTER_ACTION), (
        f"the {FILTER_STEP_ID!r} step must run {PATHS_FILTER_ACTION}…; found {uses!r}"
    )


def test_the_changes_job_runs_on_a_github_hosted_runner() -> None:
    """The detector must not run on the runner it exists to avoid paying for."""
    runner = _job(CHANGES_JOB).get("runs-on")

    assert runner == "ubuntu-latest", (
        f"the {CHANGES_JOB!r} job must run on ubuntu-latest so that deciding "
        f"whether to spend paid runner minutes costs none; found {runner!r}"
    )


def test_the_benchmark_job_waits_for_the_detector() -> None:
    """`benchmark-ratchet` must declare the `needs` edge its gate reads.

    Without it `needs.changes.outputs.bench` is empty, the gate is false for
    every pull request, and the job silently never benchmarks anything.
    """
    needs = _job(BENCHMARK_JOB).get("needs")
    assert isinstance(needs, list), f"the {BENCHMARK_JOB!r} job must declare needs"

    assert CHANGES_JOB in needs, (
        f"the {BENCHMARK_JOB!r} job must list {CHANGES_JOB!r} in `needs`; "
        f"found {needs!r}"
    )


def test_the_benchmark_job_declares_the_expected_gate() -> None:
    """The gate expression must be exactly the rule the rest of these tests model."""
    condition = _job(BENCHMARK_JOB).get("if")

    assert condition == EXPECTED_GATE, (
        f"the {BENCHMARK_JOB!r} job's `if:` must be {EXPECTED_GATE!r} — pushes to "
        "main always benchmark so the baseline artifact stays fresh, and pull "
        f"requests benchmark only on performance-relevant diffs; found {condition!r}"
    )


def test_the_filter_declares_every_performance_relevant_path() -> None:
    """The filter's path list must be exactly the performance-relevant set.

    A missing entry merges a regression unmeasured; a spurious one pays for a
    benchmark that cannot move, which is the cost this gate exists to avoid.
    """
    assert _filter_paths() == EXPECTED_FILTER_PATHS, (
        "the `bench` filter must watch exactly the performance-relevant paths; "
        f"missing {sorted(EXPECTED_FILTER_PATHS - _filter_paths())}, "
        f"unexpected {sorted(_filter_paths() - EXPECTED_FILTER_PATHS)}"
    )


def test_the_filter_uses_only_modelled_pattern_forms() -> None:
    """Every declared pattern must be a form `_matches` actually models.

    The property test below is only evidence about the real filter for as long
    as this holds; a `cuprum/**/*.py` added tomorrow would make the model
    silently over-match rather than fail.
    """
    unmodelled = sorted(
        pattern
        for pattern in _filter_paths()
        if not pattern.endswith("/**") and any(c in pattern for c in "*?[")
    )

    assert not unmodelled, (
        "these filter patterns are neither a literal path nor a `dir/**` "
        f"prefix, so `_matches` no longer models the filter: {unmodelled}"
    )


def test_the_irrelevant_paths_are_genuinely_irrelevant() -> None:
    """The property test's docs-only pool must not match the filter."""
    matched = sorted(path for path in IRRELEVANT_PATHS if _bench_output([path]))

    assert not matched, (
        "these paths are sampled as performance-irrelevant but the filter "
        f"matches them, so the property test below proves nothing: {matched}"
    )


def _relevant_paths() -> list[str]:
    """Return one concrete changed path per declared filter pattern."""
    return sorted(
        f"{pattern.removesuffix('**')}pkg/module.rs"
        if pattern.endswith("/**")
        else pattern
        for pattern in _filter_paths()
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

    assert _benchmark_runs(event_name="pull_request", bench=_bench_output(changed)), (
        f"a pull request changing {changed} must run {BENCHMARK_JOB!r}"
    )


@given(irrelevant=st.lists(st.sampled_from(IRRELEVANT_PATHS), unique=True))
def test_a_pull_request_touching_nothing_relevant_skips(irrelevant: list[str]) -> None:
    """A docs-only pull request — including an empty diff — skips the paid job."""
    assert not _benchmark_runs(
        event_name="pull_request", bench=_bench_output(irrelevant)
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
    assert _benchmark_runs(event_name=event_name, bench=_bench_output(changed)), (
        f"a {event_name!r} event changing {changed} must run {BENCHMARK_JOB!r} so "
        "the main baseline artifact is refreshed"
    )


def test_the_gate_decision_is_recorded_in_the_run_summary() -> None:
    """The `changes` job must write its verdict to the run summary.

    A skipped job and a broken gate look identical in the run list, so the
    decision — and the two inputs that produced it — has to be stated
    somewhere a maintainer auditing paid-runner spend can read it.
    """
    scripts = [
        script
        for step in _steps(CHANGES_JOB)
        if isinstance(script := step.get("run"), str)
    ]
    summary_steps = [script for script in scripts if "GITHUB_STEP_SUMMARY" in script]

    assert summary_steps, (
        f"the {CHANGES_JOB!r} job must append its gate decision to $GITHUB_STEP_SUMMARY"
    )

    script = summary_steps[0]
    for operand in ("EVENT", "BENCH", "pull_request", "run", "skip"):
        assert operand in script, (
            f"the gate summary must report {operand!r} so the recorded decision "
            f"matches the {BENCHMARK_JOB!r} gate; found:\n{script}"
        )


def test_the_workflow_serializes_runs_per_ref() -> None:
    """Concurrency must cancel superseded pull-request runs but never main runs.

    Cancelling a `main` run mid-flight abandons the baseline upload, leaving
    later pull requests comparing against a stale commit.
    """
    concurrency = _mapping(
        _workflow().get("concurrency"),
        f"{CI_WORKFLOW} must declare a concurrency policy",
    )

    assert concurrency.get("group") == "ci-${{ github.ref }}", (
        "the concurrency group must be per-ref so that runs on `main` queue "
        f"rather than race to publish the baseline; found {concurrency.get('group')!r}"
    )
    assert concurrency.get("cancel-in-progress") == (
        "${{ github.event_name == 'pull_request' }}"
    ), (
        "only pull-request runs may be cancelled; a cancelled `main` run never "
        "republishes the baseline artifact. Found "
        f"{concurrency.get('cancel-in-progress')!r}"
    )
