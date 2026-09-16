"""Read coverage-lane timers from GitHub Actions workflows.

The timeout-ordering contract uses these helpers to resolve a coverage action's
watchdogs, job ceiling, conditions, and manifest inputs without obscuring its
assertions with YAML traversal. Call :func:`_lanes` for the live lanes or
:func:`lanes_in` with a synthetic workflow to exercise the reader.
"""

from __future__ import annotations

import typing as typ

from cuprum.unittests._timeout_lane_support import (
    CEILING_MARGIN_SECONDS,
    COVERAGE_ACTION,
    COVERAGE_WORKFLOWS,
    OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS,
    CoverageLane,
    Job,
    Step,
    Workflow,
    _watchdog_of,
    _workflow,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def required_ceiling(budgets: cabc.Sequence[int]) -> int:
    """Return the smallest acceptable ceiling for one job, in seconds.

    Three terms. Each coverage step may legitimately spend its whole
    watchdog, so their sum is the floor. Measured work outside those
    windows and a margin prevent the job timer pre-empting a diagnosis.

    Parameters
    ----------
    budgets : cabc.Sequence[int]
        One watchdog budget per coverage step in the job.

    Returns
    -------
    int
        The smallest acceptable ceiling, in seconds.
    """
    return sum(budgets) + OUTSIDE_WATCHDOG_ALLOWANCE_SECONDS + CEILING_MARGIN_SECONDS


def _lanes() -> tuple[CoverageLane, ...]:
    """Return every job invoking the coverage action, with its budgets."""
    found: list[CoverageLane] = []
    for path in COVERAGE_WORKFLOWS:
        workflow = _workflow(path)
        jobs = workflow.get("jobs")
        assert isinstance(jobs, dict), f"{path} must declare a jobs mapping"
        found.extend(lanes_in(path, workflow))
    return tuple(found)


def lanes_in(path: str, workflow: Workflow) -> list[CoverageLane]:
    """Return one lane per coverage-invoking job in one workflow.

    Parameters
    ----------
    path : str
        The workflow path for failure messages.
    workflow : Workflow
        The parsed workflow document.

    Returns
    -------
    list[CoverageLane]
        Coverage jobs with their resolved watchdogs and ceiling.
    """
    found: list[CoverageLane] = []
    jobs = workflow.get("jobs")
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            steps = [
                step
                for step in (job.get("steps") or [])
                if COVERAGE_ACTION in str(step.get("uses", ""))
            ]
            if not steps:
                continue
            raw_ceiling = typ.cast("dict[str, object]", job).get("timeout-minutes")
            found.append(
                CoverageLane(
                    workflow=path,
                    job=str(name),
                    watchdogs=tuple(
                        _watchdog_of(workflow, job, step) for step in steps
                    ),
                    ceiling=None if raw_ceiling is None else int(str(raw_ceiling)),
                    conditions=tuple(
                        (
                            typ.cast("dict[str, object]", step).get("if"),
                            typ.cast("dict[str, object]", job).get("if"),
                        )
                        for step in steps
                    ),
                )
            )
    return found


def _jobs_of(workflow: Workflow) -> list[tuple[str, Job]]:
    """Return one workflow's jobs, or nothing when it declares none."""
    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict):
        return []
    return [(str(name), job) for name, job in jobs.items()]


def _coverage_steps() -> list[tuple[str, str, int, Step]]:
    """Return every step that invokes the coverage action."""
    return [
        (path, name, index + 1, step)
        for path in COVERAGE_WORKFLOWS
        for name, job in _jobs_of(_workflow(path))
        for index, step in enumerate(job.get("steps") or [])
        if COVERAGE_ACTION in str(step.get("uses", ""))
    ]


def _cargo_manifest_of(step: Step) -> object:
    """Return the ``cargo-manifest`` input a step passes, or None."""
    inputs = typ.cast("dict[str, object]", step).get("with")
    return inputs.get("cargo-manifest") if isinstance(inputs, dict) else None
