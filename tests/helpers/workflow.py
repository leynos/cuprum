"""Shared CI workflow parsing and narrow-model queries for contract tests."""

from __future__ import annotations

import typing as typ

import yaml

from .docs import repo_root
from .workflow_gate import bench_output, benchmark_runs, matches_filter
from .workflow_queries import first_step_running
from .workflow_shell import script_runs_command

__all__ = (
    "bench_output",
    "benchmark_runs",
    "first_step_running",
    "matches_filter",
    "script_runs_command",
)
if typ.TYPE_CHECKING:
    import collections.abc as cabc
CI_WORKFLOW = ".github/workflows/ci.yml"
CHANGES_JOB = "changes"
BENCHMARK_JOB = "benchmark-ratchet"
FILTER_STEP_ID = "filter"
FILTER_NAME = "bench"


class Step(typ.TypedDict, total=False):
    """A workflow step with keys represented in the narrow test model.

    Attributes
    ----------
    id : object
        Identifier used to locate the step within its job.
    uses : object
        Action or reusable workflow invoked by the step.
    run : object
        Shell script executed by the step.
    """

    id: object
    uses: object
    run: object


class Job(typ.TypedDict, total=False):
    """A workflow job with keys represented in the narrow test model.

    Attributes
    ----------
    needs : object
        Job or jobs that must complete before this job starts.
    outputs : object
        Values exposed by the job to downstream jobs.
    steps : list[Step]
        Steps executed by the job, when it does not call a reusable workflow.
    """

    needs: object
    outputs: object
    steps: list[Step]


class Workflow(typ.TypedDict, total=False):
    """A parsed workflow with keys represented in the narrow test model.

    Attributes
    ----------
    concurrency : object
        Concurrency configuration declared by the workflow.
    jobs : dict[str, Job]
        Jobs declared by the workflow, keyed by job name.
    """

    concurrency: object
    jobs: dict[str, Job]


def _require(*, condition: bool, message: str) -> None:
    """Raise ``AssertionError`` when a shape requirement is unmet."""
    if not condition:
        raise AssertionError(message)


def mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a workflow value to a string-keyed mapping.

    `yaml.safe_load` produces mappings of unknown key type, which makes every
    subsequent `.get("…")` a type error rather than a narrowing.

    Parameters
    ----------
    value : object
        Value read from the workflow document.
    message : str
        Diagnostic message to include when ``value`` is not a mapping.

    Returns
    -------
    dict[str, object]
        The narrowed mapping.

    Raises
    ------
    AssertionError
        If ``value`` is not a mapping.
    """  # noqa: DOC502 - contract validation delegates to _require.
    _require(condition=isinstance(value, dict), message=message)
    return typ.cast("dict[str, object]", value)


def workflow() -> Workflow:
    """Read and parse the repository's Continuous Integration workflow.

    Returns
    -------
    Workflow
        The parsed workflow represented by the narrow contract-test model.

    Raises
    ------
    AssertionError
        If the workflow does not parse to a mapping.
    """  # noqa: DOC502 - contract validation delegates to _require.
    source = (repo_root() / CI_WORKFLOW).read_text(encoding="utf-8")
    return parse_workflow(source)


def parse_workflow(source: str) -> Workflow:
    """Parse workflow YAML source into the narrow contract-test model.

    Parameters
    ----------
    source : str
        YAML document containing the workflow definition.

    Returns
    -------
    Workflow
        The parsed workflow represented by the narrow contract-test model.

    Raises
    ------
    AssertionError
        If the parsed document is not a mapping.
    """  # noqa: DOC502 - contract validation delegates to _require.
    parsed = yaml.safe_load(source)
    _require(
        condition=isinstance(parsed, dict),
        message=f"{CI_WORKFLOW} must parse to a mapping",
    )
    return typ.cast("Workflow", parsed)


def job(workflow_data: Workflow, job_name: str) -> dict[str, object]:
    """Return a named job, reporting the available names when it is absent.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the job.
    job_name : str
        Name of the job to return.

    Returns
    -------
    dict[str, object]
        The named job payload.

    Raises
    ------
    AssertionError
        If the jobs mapping or named job is absent or malformed.
    """  # noqa: DOC502 - contract validation delegates to _require.
    jobs = mapping(
        workflow_data.get("jobs"), f"{CI_WORKFLOW} must declare a jobs mapping"
    )
    return mapping(
        jobs.get(job_name),
        f"{CI_WORKFLOW} must declare a {job_name!r} job; found {sorted(jobs)}",
    )


def steps(workflow_data: Workflow, job_name: str) -> list[dict[str, object]]:
    """Return the declared steps of a named job.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the job.
    job_name : str
        Name of the job whose steps are required.

    Returns
    -------
    list[dict[str, object]]
        The job's step payloads in declaration order.

    Raises
    ------
    AssertionError
        If the named job or its declared steps are malformed or absent.
    """  # noqa: DOC502 - contract validation delegates to _require.
    declared = job(workflow_data, job_name).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"the {job_name!r} job must declare steps",
    )
    return typ.cast("list[dict[str, object]]", declared)


def step_with_id(
    workflow_data: Workflow, job_name: str, step_id: str
) -> dict[str, object]:
    """Return the step of a job carrying the requested ``id:``.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the job.
    job_name : str
        Name of the job to search.
    step_id : str
        Identifier of the step to return.

    Returns
    -------
    dict[str, object]
        The matching step payload.

    Raises
    ------
    AssertionError
        If the named job has no steps or no step carries ``step_id``.
    """  # noqa: DOC502 - contract validation delegates to _require.
    found = next(
        (step for step in steps(workflow_data, job_name) if step.get("id") == step_id),
        None,
    )
    return mapping(
        found, f"the {job_name!r} job must declare a step with id {step_id!r}"
    )


def step_named(
    workflow_data: Workflow, job_name: str, step_name: str
) -> dict[str, object]:
    """Return the step of a job carrying the requested ``name:``.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the job.
    job_name : str
        Name of the job to search.
    step_name : str
        Name of the step to return.

    Returns
    -------
    dict[str, object]
        The matching step payload.

    Raises
    ------
    AssertionError
        If the named job has no steps or no step carries ``step_name``.
    """  # noqa: DOC502 - contract validation delegates to _require.
    found = next(
        (
            step
            for step in steps(workflow_data, job_name)
            if step.get("name") == step_name
        ),
        None,
    )
    names = [step.get("name") for step in steps(workflow_data, job_name)]
    return mapping(
        found,
        f"the {job_name!r} job must declare a step named {step_name!r}; found {names}",
    )


def script_of(step: cabc.Mapping[str, object]) -> str | None:
    """Return a step's ``run:`` script, or ``None`` when it runs no script.

    Parameters
    ----------
    step : collections.abc.Mapping[str, object]
        Step payload to inspect.

    Returns
    -------
    str | None
        The step's script when ``run`` is a string; otherwise, ``None``.
    """
    script = step.get("run")
    return script if isinstance(script, str) else None


def _declared_steps(job_payload: object, *, job_name: str) -> list[dict[str, object]]:
    """Return a job's declared steps, or an empty list when it has none."""
    declared = mapping(job_payload, f"job {job_name!r}").get("steps")
    if not isinstance(declared, list):
        return []
    return typ.cast("list[dict[str, object]]", declared)


def run_scripts(workflow_data: Workflow) -> cabc.Iterator[tuple[str, str]]:
    """Yield each job name and script from ``run:`` steps in the workflow.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow whose jobs should be inspected.

    Yields
    ------
    tuple[str, str]
        The containing job name and the step's shell script.

    Raises
    ------
    AssertionError
        If the workflow's jobs mapping or a job payload is malformed.
    """  # noqa: DOC502 - contract validation delegates to _require.
    jobs = mapping(
        workflow_data.get("jobs"), f"{CI_WORKFLOW} must declare a jobs mapping"
    )
    for job_name, job_payload in jobs.items():
        for step in _declared_steps(job_payload, job_name=job_name):
            if (script := script_of(step)) is not None:
                yield job_name, script


def benchmark_gate(workflow_data: Workflow) -> str:
    """Return the ``if:`` expression gating the benchmark job.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the benchmark job.

    Returns
    -------
    str
        The benchmark job's gating expression.

    Raises
    ------
    AssertionError
        If the benchmark job or its string ``if:`` condition is malformed or
        absent.
    """  # noqa: DOC502 - contract validation delegates to _require.
    condition = job(workflow_data, BENCHMARK_JOB).get("if")
    _require(
        condition=isinstance(condition, str),
        message=f"the {BENCHMARK_JOB!r} job must declare an `if:` condition",
    )
    return typ.cast("str", condition)


def filter_paths(workflow_data: Workflow) -> frozenset[str]:
    """Return the path patterns declared by the ``bench`` filter.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the changes job and filter step.

    Returns
    -------
    frozenset[str]
        Declared literal and directory-prefix path patterns.

    Raises
    ------
    AssertionError
        If the filter step, inputs, or ``bench`` pattern list is malformed or
        absent.
    """  # noqa: DOC502 - contract validation delegates to _require.
    step = step_with_id(workflow_data, CHANGES_JOB, FILTER_STEP_ID)
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
        condition=isinstance(patterns, list),
        message=(
            f"the filter must declare a {FILTER_NAME!r} list; found {sorted(filters)}"
        ),
    )
    return frozenset(str(pattern) for pattern in typ.cast("list[object]", patterns))
