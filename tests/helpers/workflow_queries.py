"""Queries over the narrow CI workflow model."""

from __future__ import annotations

import typing as typ

from .workflow_shell import script_runs_command

if typ.TYPE_CHECKING:
    from .workflow import Workflow


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
    """  # noqa: DOC502 - contract validation delegates to _require.
    from .workflow import _require, script_of, steps

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
