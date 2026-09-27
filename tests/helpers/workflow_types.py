"""Narrow types shared by Continuous Integration workflow contract tests."""

from __future__ import annotations

import typing as typ

# `with` and `if` are Python keywords, so these keys can only be declared
# through the functional TypedDict form and mixed in as a base class.
_StepKeywords = typ.TypedDict(
    "_StepKeywords", {"with": object, "if": object}, total=False
)


class Step(_StepKeywords, total=False):
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
    if : object
        Condition guarding the step. Read by the many contracts that resolve a
        guard, and written by the ones that seed a re-gated step as a fault, so
        the model carries it rather than leaving those writes unrepresentable.
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
    env : object
        Environment variables and expressions declared for the whole job.
    needs : object
        Job or jobs that must complete before this job starts.
    outputs : object
        Values exposed by the job to downstream jobs.
    runs-on : object
        Runner label or expression selecting the job's runner.
    steps : list[Step]
        Steps executed by the job, when it does not call a reusable workflow.
    """

    env: object
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
