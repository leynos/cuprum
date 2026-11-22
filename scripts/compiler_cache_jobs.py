"""Join compiler-cache records to GitHub's jobs API.

The jobs API does not expose the workflow's job key, only its display name, so
the join to a record's ``job`` is declared through a mapping whose value may
contain a ``{leg}`` placeholder. A record the mapping cannot place keeps an
``unknown`` result and contributes no whole-job seconds rather than a guess.

Examples
--------
>>> job_result({"conclusion": "success"})
'passed'
>>> job_result({"conclusion": "cancelled"})
'failed'
"""

from __future__ import annotations

import datetime as dt
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def instant(value: object) -> dt.datetime | None:
    """Parse one GitHub timestamp, or return ``None``."""
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def job_seconds(job: cabc.Mapping[str, object]) -> float | None:
    """Return one API job's wall time in seconds, or ``None`` if incomplete."""
    started = instant(job.get("started_at"))
    completed = instant(job.get("completed_at"))
    if started is None or completed is None:
        return None
    seconds = (completed - started).total_seconds()
    return round(seconds, 3) if seconds >= 0 else None


def job_result(job: cabc.Mapping[str, object]) -> str:
    """Map a jobs API conclusion onto the experiment's closed vocabulary."""
    conclusion = str(job.get("conclusion") or "")
    if conclusion == "success":
        return "passed"
    if conclusion in {"failure", "timed_out", "cancelled", "action_required"}:
        return "failed"
    if conclusion in {"skipped", "neutral"}:
        return "not-run"
    return "unknown"


def matching_job(
    record: cabc.Mapping[str, object],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> cabc.Mapping[str, object] | None:
    """Return the API job a record describes, through the declared mapping."""
    template = names.get(str(record["job"]))
    if template is None:
        return None
    pattern = template.replace("{leg}", str(record["matrix_leg"]))
    for job in jobs:
        if str(job.get("run_id")) != str(record["run_id"]):
            continue
        attempt = job.get("run_attempt")
        if attempt is not None and str(attempt) != str(record["run_attempt"]):
            continue
        if pattern in str(job.get("name", "")):
            return job
    return None


def seconds_and_result(
    record: cabc.Mapping[str, object],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> tuple[float | None, str]:
    """Join one record to its API job and return its seconds and result."""
    job = matching_job(record, jobs, names)
    if job is None:
        return None, "unknown"
    return job_seconds(job), job_result(job)
