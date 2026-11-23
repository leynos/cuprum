"""Whole-job cost accounting for the compiler-cache experiment.

The evidence records carry per-bracket timings; this module turns them into
the paid whole-job view the adoption gate compares. A job with several
brackets is charged once, the critical path is the longest run's span, and a
missing jobs document leaves every figure unknown rather than zero.

Examples
--------
>>> console_number(
...     {"incremental_cost_seconds": -5}, "incremental_cost_seconds", signed=True
... )
-5.0
>>> console_number(None, "incremental_cost_seconds")
"""

from __future__ import annotations

import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_validation as validation

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt


def cost(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
    console: cabc.Mapping[str, object] | None,
) -> dict[str, object]:
    """Return paid job-seconds per workflow and the critical path.

    A job with several brackets is counted once: the records are deduplicated
    by workflow, run, attempt, job, leg, and bracket before the seconds are
    summed, so three typecheck brackets do not treble one job's cost.

    Returns
    -------
    dict[str, object]
        Paid seconds and critical path per workflow, the billing label, and
        the console's population, transfer, and incremental cost fields.
    """
    per_job: dict[tuple[str, str, str, str], float | None] = {}
    for record in records:
        job = jobs_reader.matching_job(record, jobs, names)
        key = (
            str(record["workflow"]),
            str(record["run_id"]),
            str(record["run_attempt"]),
            f"{record['job']}/{record['matrix_leg']}",
        )
        per_job[key] = jobs_reader.job_seconds(job) if job is not None else None
    totals: dict[str, float] = {}
    incomplete: set[str] = set()
    for (workflow, _run, _attempt, _key), seconds in per_job.items():
        if seconds is None:
            incomplete.add(workflow)
            continue
        totals[workflow] = round(totals.get(workflow, 0.0) + seconds, 3)
    by_workflow: dict[str, float | None] = {
        workflow: None if workflow in incomplete else total
        for workflow, total in totals.items()
    } | {workflow: None for workflow in incomplete}
    critical = critical_paths(records, jobs, names)
    verified = console is not None and console.get("billing_rounding") == "verified"
    return {
        "paid_job_seconds_by_workflow": by_workflow or None,
        "critical_path_seconds_by_workflow": critical or None,
        "billing": "verified" if verified else "proxy",
        "initial_population_seconds": console_number(
            console, "initial_population_seconds"
        ),
        "incremental_transfer_seconds": console_number(
            console, "incremental_transfer_seconds"
        ),
        "incremental_cost_seconds": console_number(
            console, "incremental_cost_seconds", signed=True
        ),
    }


def critical_paths(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, float]:
    """Return each workflow's longest run, measured from job timestamps.

    Returns
    -------
    dict[str, float]
        The longest run span per workflow, in seconds, or an empty mapping
        when no record could be joined to the jobs document.
    """
    runs: dict[tuple[str, str], set[str]] = {}
    for record in records:
        identity = (str(record["run_id"]), str(record["run_attempt"]))
        runs.setdefault(identity, set()).add(str(record["workflow"]))
    spans: dict[tuple[str, str, str], list[dt.datetime]] = {
        (workflow, run_id, attempt): []
        for (run_id, attempt), workflows in runs.items()
        for workflow in workflows
    }
    for job in jobs:
        identity = (str(job.get("run_id")), str(job.get("run_attempt")))
        workflows = runs.get(identity)
        if workflows is None:
            continue
        started = jobs_reader.instant(job.get("started_at"))
        completed = jobs_reader.instant(job.get("completed_at"))
        if started is None or completed is None:
            continue
        for workflow in workflows:
            spans[workflow, identity[0], identity[1]].extend([started, completed])
    critical: dict[str, float] = {}
    for (workflow, _run, _attempt), instants in spans.items():
        if not instants:
            continue
        span = (max(instants) - min(instants)).total_seconds()
        critical[workflow] = round(max(critical.get(workflow, 0.0), span), 3)
    return critical


def console_number(
    console: cabc.Mapping[str, object] | None, name: str, *, signed: bool = False
) -> float | None:
    """Return one finite numeric console field.

    Parameters
    ----------
    console : collections.abc.Mapping[str, object] | None
        The dated console observation, or ``None``.
    name : str
        The field to read.
    signed : bool
        Accept a negative value. Durations are non-negative; an incremental
        cost may be negative when the change saves money.

    Returns
    -------
    float | None
        The field, or ``None`` when the console is absent, the field is absent,
        or the value is not a finite number.
    """
    if console is None:
        return None
    value = console.get(name)
    if not validation.number(value):
        return None
    number = float(typ.cast("float", value))
    return number if signed or number >= 0 else None
