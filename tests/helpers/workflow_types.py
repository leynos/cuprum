"""Narrow types shared by Continuous Integration workflow contract tests."""

from __future__ import annotations

import typing as typ

# `with` is a Python keyword, so this key can only be declared through the
# functional TypedDict form and mixed in as a base class.
_StepInputs = typ.TypedDict("_StepInputs", {"with": object}, total=False)


class Step(_StepInputs, total=False):
    """A workflow step with keys represented in the narrow test model.

    Attributes
    ----------
    id : object
        Identifier used to locate the step within its job.
    name : object
        Display name used to locate the step within its job.
    uses : object
        Action or reusable workflow invoked by the step.
    run : object
        Shell script executed by the step.
    with : object
        Input mapping passed to the invoked action.
    """

    id: object
    name: object
    uses: object
    run: object


# `runs-on` is not a Python identifier, so it needs the functional form too.
_JobRunner = typ.TypedDict("_JobRunner", {"runs-on": object}, total=False)


class Job(_JobRunner, total=False):
    """A workflow job with keys represented in the narrow test model.

    Attributes
    ----------
    needs : object
        Job or jobs that must complete before this job starts.
    outputs : object
        Values exposed by the job to downstream jobs.
    runs-on : object
        Runner label or expression selecting the job's runner.
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
