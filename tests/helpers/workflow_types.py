"""Narrow types shared by Continuous Integration workflow contract tests."""

from __future__ import annotations

import typing as typ


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
