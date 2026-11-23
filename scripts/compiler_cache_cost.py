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


def _job_key(record: cabc.Mapping[str, object]) -> tuple[str, str, str, str]:
    """Return the identity one job is charged under, however many brackets it has."""
    return (
        str(record["workflow"]),
        str(record["run_id"]),
        str(record["run_attempt"]),
        f"{record['job']}/{record['matrix_leg']}",
    )


def _per_job_seconds(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[tuple[str, str, str, str], float | None]:
    """Return each distinct job's whole-job seconds, or ``None`` when unjoined."""
    per_job: dict[tuple[str, str, str, str], float | None] = {}
    for record in records:
        job = jobs_reader.matching_job(record, jobs, names)
        per_job[_job_key(record)] = (
            jobs_reader.job_seconds(job) if job is not None else None
        )
    return per_job


def _priced_totals(
    per_job: cabc.Mapping[tuple[str, str, str, str], float | None],
) -> dict[str, float]:
    """Return the summed seconds of the jobs that could be priced."""
    totals: dict[str, float] = {}
    for (workflow, _run, _attempt, _key), seconds in per_job.items():
        if seconds is not None:
            totals[workflow] = round(totals.get(workflow, 0.0) + seconds, 3)
    return totals


def _unpriced_workflows(
    per_job: cabc.Mapping[tuple[str, str, str, str], float | None],
) -> set[str]:
    """Return the workflows holding at least one job that could not be priced."""
    return {
        workflow
        for (workflow, _run, _attempt, _key), seconds in per_job.items()
        if seconds is None
    }


def _workflow_totals(
    per_job: cabc.Mapping[tuple[str, str, str, str], float | None],
) -> dict[str, float | None]:
    """Return each workflow's total, made unknown by any unpriced job."""
    priced = _priced_totals(per_job)
    return priced | dict.fromkeys(_unpriced_workflows(per_job))


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
    per_job = _per_job_seconds(records, jobs, names)
    by_workflow = _workflow_totals(per_job)
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
    runs = _run_workflows(records)
    return _critical_spans(_run_instants(jobs, runs))


def _run_workflows(
    records: cabc.Sequence[cabc.Mapping[str, object]],
) -> dict[tuple[str, str], set[str]]:
    """Return the workflows each run's records belong to."""
    runs: dict[tuple[str, str], set[str]] = {}
    for record in records:
        identity = (str(record["run_id"]), str(record["run_attempt"]))
        runs.setdefault(identity, set()).add(str(record["workflow"]))
    return runs


def _job_instants(
    job: cabc.Mapping[str, object],
) -> tuple[dt.datetime, dt.datetime] | None:
    """Return a job's start and completion, or ``None`` when either is missing."""
    started = jobs_reader.instant(job.get("started_at"))
    completed = jobs_reader.instant(job.get("completed_at"))
    if started is None or completed is None:
        return None
    return started, completed


def _run_instants(
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    runs: cabc.Mapping[tuple[str, str], set[str]],
) -> dict[tuple[str, str, str], list[dt.datetime]]:
    """Return every timestamp of each instrumented run's jobs, per workflow."""
    spans: dict[tuple[str, str, str], list[dt.datetime]] = {
        (workflow, run_id, attempt): []
        for (run_id, attempt), workflows in runs.items()
        for workflow in workflows
    }
    for job in jobs:
        identity = (str(job.get("run_id")), str(job.get("run_attempt")))
        workflows = runs.get(identity)
        instants = _job_instants(job)
        if workflows is None or instants is None:
            continue
        for workflow in workflows:
            spans[workflow, *identity].extend(instants)
    return spans


def _critical_spans(
    spans: cabc.Mapping[tuple[str, str, str], list[dt.datetime]],
) -> dict[str, float]:
    """Return each workflow's longest span, ignoring runs with no timestamps."""
    critical: dict[str, float] = {}
    for (workflow, _run, _attempt), instants in spans.items():
        if instants:
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
