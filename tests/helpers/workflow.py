"""Shared helpers for reading the CI workflow back in contract tests.

Two suites assert the path gate in front of `benchmark-ratchet`: the unit
contract tests in `cuprum/unittests/test_benchmark_gate_ci_contract.py`, which
pin the declarations that make up the gate, and the behavioural tests in
`tests/behaviour/test_benchmark_path_gate_behaviour.py`, which state the
decision it produces for a given pull request. Both must read the same
`ci.yml` and model it the same way, so the reading and the model live here
rather than in either suite.

`yaml.safe_load` returns `typing.Any`, which erases every mistake an assertion
can make about the shape it reads: a misspelled key yields `None`, and the
assertion above it then passes or fails for a reason unrelated to the
contract. The shapes below declare the keys these helpers reach for, so a typo
is a type error. Their *values* stay `object`, because they come from a file
this suite does not control, and are narrowed where they are read.
"""

from __future__ import annotations

import functools
import typing as typ

import yaml

from .docs import repo_root

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CI_WORKFLOW = ".github/workflows/ci.yml"

CHANGES_JOB = "changes"
BENCHMARK_JOB = "benchmark-ratchet"
FILTER_STEP_ID = "filter"
FILTER_NAME = "bench"


class Step(typ.TypedDict, total=False):
    """One step of a job, declaring only the keys these helpers read."""

    id: object
    uses: object
    run: object


class Job(typ.TypedDict, total=False):
    """One job of a workflow, declaring only the keys these helpers read."""

    needs: object
    outputs: object
    steps: list[Step]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow file, declaring only the keys these helpers read."""

    concurrency: object
    jobs: dict[str, Job]


class WorkflowContractError(AssertionError):
    """The workflow does not have the shape the contract tests require.

    Raised rather than asserted so that a malformed workflow fails with the
    same message whichever suite read it, and so that a suite can distinguish
    "the file is not shaped like a workflow" from "the contract is not met".
    """


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    """Raise `WorkflowContractError` when a shape requirement is unmet."""
    if not condition:
        raise WorkflowContractError(message)


def mapping(value: object, message: str) -> dict[str, object]:
    """Require that a value read from the workflow is a mapping, and type it.

    `yaml.safe_load` produces mappings of unknown key type, which makes every
    subsequent `.get("…")` a type error rather than a narrowing.
    """
    _require(isinstance(value, dict), message)
    return typ.cast("dict[str, object]", value)


@functools.cache
def workflow() -> Workflow:
    """Parse the CI workflow."""
    parsed = yaml.safe_load((repo_root() / CI_WORKFLOW).read_text(encoding="utf-8"))
    _require(isinstance(parsed, dict), f"{CI_WORKFLOW} must parse to a mapping")
    return typ.cast("Workflow", parsed)


def job(job_name: str) -> dict[str, object]:
    """Return a named job, failing with the available names when absent."""
    jobs = mapping(workflow().get("jobs"), f"{CI_WORKFLOW} must declare a jobs mapping")
    return mapping(
        jobs.get(job_name),
        f"{CI_WORKFLOW} must declare a {job_name!r} job; found {sorted(jobs)}",
    )


def steps(job_name: str) -> list[dict[str, object]]:
    """Return the steps of a named job."""
    declared = job(job_name).get("steps")
    _require(isinstance(declared, list), f"the {job_name!r} job must declare steps")
    return typ.cast("list[dict[str, object]]", declared)


def step_with_id(job_name: str, step_id: str) -> dict[str, object]:
    """Return the step of a job carrying a given `id:`."""
    found = next(
        (step for step in steps(job_name) if step.get("id") == step_id),
        None,
    )
    return mapping(
        found, f"the {job_name!r} job must declare a step with id {step_id!r}"
    )


def benchmark_gate() -> str:
    """Return the `if:` expression gating the benchmark job."""
    condition = job(BENCHMARK_JOB).get("if")
    _require(
        isinstance(condition, str),
        f"the {BENCHMARK_JOB!r} job must declare an `if:` condition",
    )
    return typ.cast("str", condition)


@functools.cache
def filter_paths() -> frozenset[str]:
    """Return the path patterns the `bench` filter declares."""
    step = step_with_id(CHANGES_JOB, FILTER_STEP_ID)
    inputs = mapping(
        step.get("with"),
        f"the {FILTER_STEP_ID!r} step must pass inputs to the filter action",
    )
    filters = mapping(
        yaml.safe_load(str(inputs["filters"])),
        "the `filters` input must parse to a mapping",
    )
    patterns = filters.get(FILTER_NAME)
    _require(
        isinstance(patterns, list),
        f"the filter must declare a {FILTER_NAME!r} list; found {sorted(filters)}",
    )
    return frozenset(str(pattern) for pattern in typ.cast("list[object]", patterns))


def matches_filter(pattern: str, path: str) -> bool:
    """Return whether a changed `path` matches a declared filter `pattern`.

    A bounded model of the two pattern forms the filter is allowed to use: a
    literal path, and a `dir/**` prefix. A contract test fails when a pattern
    outside those forms is declared, so the model cannot silently stop
    describing the filter it stands in for.
    """
    if pattern.endswith("/**"):
        return path.startswith(pattern.removesuffix("**"))
    return path == pattern


def bench_output(changed_paths: cabc.Collection[str]) -> bool:
    """Model the `bench` output the filter produces for a set of changes."""
    return any(
        matches_filter(pattern, path)
        for pattern in filter_paths()
        for path in changed_paths
    )


def benchmark_runs(*, event_name: str, bench: bool) -> bool:
    """Model the gate, returning whether `benchmark-ratchet` runs.

    Mirrors the `if:` expression a contract test pins verbatim; the pin is what
    keeps this model and the workflow from drifting apart.
    """
    return event_name != "pull_request" or bench
