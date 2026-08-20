"""Shared helpers for reading the CI workflow back in contract tests.

Several suites read `ci.yml` back: the unit contract tests in
`cuprum/unittests/test_benchmark_gate_ci_contract.py`, which pin the
declarations making up the path gate; the behavioural tests in
`tests/behaviour/test_benchmark_path_gate_behaviour.py` and
`test_benchmark_gate_summary_behaviour.py`, which state the decision those
declarations produce and run the script that records it; and
`cuprum/unittests/test_extension_ci_contract.py`, which asserts how the same
workflow builds the extension. They must all read one file the same way, so
the parsing and the path model live here rather than in any of them. Keep it
that way: a second parser drifts from the first, and then two suites disagree
about what the same file says.

`yaml.safe_load` returns `typing.Any`, which erases every mistake an assertion
can make about the shape it reads: a misspelled key yields `None`, and the
assertion above it then passes or fails for a reason unrelated to the
contract. The shapes below declare the keys these helpers reach for, so a typo
is a type error. Their *values* stay `object`, because they come from a file
this suite does not control, and are narrowed where they are read.
"""

from __future__ import annotations

import functools
import shlex
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


def _require(condition: bool, message: str) -> None:  # noqa: FBT001
    """Raise `AssertionError` when a shape requirement is unmet.

    Plain `AssertionError`, not a bespoke subclass: no caller distinguishes
    "the file is not shaped like a workflow" from any other failed assertion,
    and pytest reports both identically.
    """
    if not condition:
        raise AssertionError(message)


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


def step_named(job_name: str, step_name: str) -> dict[str, object]:
    """Return the step of a job carrying a given `name:`."""
    found = next(
        (step for step in steps(job_name) if step.get("name") == step_name),
        None,
    )
    names = [step.get("name") for step in steps(job_name)]
    return mapping(
        found,
        f"the {job_name!r} job must declare a step named {step_name!r}; found {names}",
    )


def script_of(step: cabc.Mapping[str, object]) -> str | None:
    """Return a step's ``run:`` script, or None when it runs no script."""
    script = step.get("run")
    return script if isinstance(script, str) else None


def _declared_steps(job_payload: object, *, job_name: str) -> list[dict[str, object]]:
    """Return a job's steps, or none for a job that declares no steps.

    A job that calls a reusable workflow declares `uses:` and no steps, which
    is a legitimate shape rather than a contract failure.
    """
    declared = mapping(job_payload, f"job {job_name!r}").get("steps")
    if not isinstance(declared, list):
        return []
    return typ.cast("list[dict[str, object]]", declared)


def run_scripts() -> cabc.Iterator[tuple[str, str]]:
    """Yield the job name and script of every ``run:`` step in the workflow."""
    jobs = mapping(workflow().get("jobs"), f"{CI_WORKFLOW} must declare a jobs mapping")
    for job_name, job_payload in jobs.items():
        for step in _declared_steps(job_payload, job_name=job_name):
            if (script := script_of(step)) is not None:
                yield job_name, script


def _is_environment_assignment(token: str) -> bool:
    """Return whether `token` is a leading shell environment assignment."""
    if "=" not in token:
        return False
    name, _ = token.split("=", maxsplit=1)
    return name.isidentifier()


def _command_segments(script: str) -> cabc.Iterator[list[str]]:
    """Yield shell-token segments split at command boundaries."""
    boundaries = frozenset({
        "&",
        "&&",
        ";",
        "|",
        "||",
        "if",
        "then",
        "elif",
        "else",
        "do",
    })

    for line in script.replace("\\\n", " ").splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
        segment_start = 0

        for index, token in enumerate([*tokens, ";"]):
            if token not in boundaries:
                continue
            yield tokens[segment_start:index]
            segment_start = index + 1


def _segment_starts_command(segment: list[str], expected: tuple[str, ...]) -> bool:
    """Return whether a shell segment starts with the expected command."""
    while segment and _is_environment_assignment(segment[0]):
        segment.pop(0)
    return tuple(segment[: len(expected)]) == expected


def script_runs_command(script: str, command: str) -> bool:
    """Return whether `script` executes `command` as leading shell tokens."""
    expected = tuple(shlex.split(command))
    return any(
        _segment_starts_command(segment, expected)
        for segment in _command_segments(script)
    )


def first_step_running(command: str, *, job_name: str) -> tuple[int, str]:
    """Return the position and script of the first step running `command`."""
    found = next(
        (
            (index, script)
            for index, step in enumerate(steps(job_name))
            if (script := script_of(step)) is not None
            and script_runs_command(script, command)
        ),
        None,
    )
    _require(found is not None, f"no step in the {job_name!r} job runs {command!r}")
    return typ.cast("tuple[int, str]", found)


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
