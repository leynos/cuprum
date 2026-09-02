"""Shared CI workflow parsing and narrow-model queries for contract tests."""

from __future__ import annotations

import typing as typ

import yaml

from .docs import repo_root
from .workflow_gate import bench_output, benchmark_runs, matches_filter
from .workflow_shell import script_runs_command
from .workflow_types import Job, Step, Workflow

# fmt: off
__all__ = ("Job", "Step", "Workflow", "bench_output", "benchmark_runs",
           "first_step_running", "matches_filter", "script_runs_command")
# fmt: on
if typ.TYPE_CHECKING:
    import collections.abc as cabc
CI_WORKFLOW = ".github/workflows/ci.yml"
CHANGES_JOB = "changes"
BENCHMARK_JOB = "benchmark-ratchet"
FILTER_STEP_ID = "filter"
FILTER_NAME = "bench"


def _require(*, condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def mapping(value: object, message: str) -> dict[str, object]:
    """Narrow a workflow value to a string-keyed mapping.

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
        If ``value`` is not a mapping with only string keys.
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    _require(
        condition=isinstance(value, dict)
        and all(isinstance(key, str) for key in value),
        message=message,
    )
    return typ.cast("dict[str, object]", value)


def read_workflow_source() -> str:
    """Read the repository's Continuous Integration workflow source.

    Returns
    -------
    str
        UTF-8 source text from the checked-in workflow.

    Raises
    ------
    OSError
        If the workflow source cannot be read.
    """  # ruff: ignore[docstring-extraneous-exception] - read_text propagates OSError.
    return (repo_root() / CI_WORKFLOW).read_text(encoding="utf-8")


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
    yaml.YAMLError
        If ``source`` is not valid YAML.
    """  # ruff: ignore[docstring-extraneous-exception] - parser and contract validator exceptions propagate.
    parsed = yaml.safe_load(source)
    boolean_on_key = (
        next((key for key in parsed if key is True), None)
        if isinstance(parsed, dict)
        else None
    )
    if boolean_on_key is not None:
        parsed["on"] = parsed.pop(boolean_on_key)
    return typ.cast(
        "Workflow", mapping(parsed, f"{CI_WORKFLOW} must parse to a mapping")
    )


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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    declared = job(workflow_data, job_name).get("steps")
    _require(
        condition=isinstance(declared, list),
        message=f"the {job_name!r} job must declare steps",
    )
    for step in typ.cast("list[object]", declared):
        mapping(step, f"the {job_name!r} job must declare mapping steps")
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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    found, _ = _step_matching(workflow_data, job_name, "id", step_id)
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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    found, declared_steps = _step_matching(workflow_data, job_name, "name", step_name)
    names = [step.get("name") for step in declared_steps]
    return mapping(
        found,
        f"the {job_name!r} job must declare a step named {step_name!r}; found {names}",
    )


def _step_matching(
    workflow_data: Workflow, job_name: str, field_name: str, requested_value: str
) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    declared_steps = steps(workflow_data, job_name)
    found = next(
        (step for step in declared_steps if step.get(field_name) == requested_value),
        None,
    )
    return found, declared_steps


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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    jobs = mapping(
        workflow_data.get("jobs"), f"{CI_WORKFLOW} must declare a jobs mapping"
    )
    for job_name, job_payload in jobs.items():
        for step in _declared_steps(job_payload, job_name=job_name):
            if (script := script_of(step)) is not None:
                yield job_name, script


def first_step_running(
    workflow_data: Workflow, command: str, *, job_name: str
) -> tuple[int, str]:
    """Return the first step in a job that runs ``command``.

    Parameters
    ----------
    workflow_data : Workflow
        Parsed workflow containing the job to search.
    command : str
        Command whose token sequence must begin a step's script segment.
    job_name : str
        Name of the job to search.

    Returns
    -------
    tuple[int, str]
        The zero-based step position and its matching shell script.

    Raises
    ------
    AssertionError
        If no step in the named job runs ``command``.
    ValueError
        If a step script contains unclosed shell quoting.
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    found = next(
        (
            (index, script)
            for index, step in enumerate(steps(workflow_data, job_name))
            if (script := script_of(step)) is not None
            and script_runs_command(script, command)
        ),
        None,
    )
    _require(
        condition=found is not None,
        message=f"no step in the {job_name!r} job runs {command!r}",
    )
    return typ.cast("tuple[int, str]", found)


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
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    condition = job(workflow_data, BENCHMARK_JOB).get("if")
    _require(
        condition=isinstance(condition, str),
        message=f"the {BENCHMARK_JOB!r} job must declare an `if:` condition",
    )
    return typ.cast("str", condition)


def filter_paths(workflow_data: Workflow) -> frozenset[str]:
    """Return the path patterns declared by the ``bench`` filter.

    Returns
    -------
    frozenset[str]
        Declared literal and directory-prefix path patterns.

    Raises
    ------
    AssertionError
        If the filter step, inputs, or ``bench`` pattern list is malformed or
        absent.
    yaml.YAMLError
        If the ``filters`` input is not valid YAML.
    """  # ruff: ignore[docstring-extraneous-exception] - contract validation delegates to _require.
    step = step_with_id(workflow_data, CHANGES_JOB, FILTER_STEP_ID)
    inputs = mapping(
        step.get("with"),
        f"the {FILTER_STEP_ID!r} step must pass inputs to the filter action",
    )
    filters_input = inputs.get("filters")
    _require(
        condition=isinstance(filters_input, str),
        message=f"{CHANGES_JOB!r} {FILTER_STEP_ID!r}: filters must be a string",
    )
    filters = mapping(
        yaml.safe_load(typ.cast("str", filters_input)),
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
