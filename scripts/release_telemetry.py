#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Write one bounded JSONL record for a release phase, failing open.

``release.yml`` runs this at each release decision point, after the phase's
steps and even when they failed, following the benchmark-gate telemetry
precedent (ADR-014, ``docs/ci-benchmark-gate-telemetry.md``). Every label comes
from a closed vocabulary; the run identity, recorded time, and tag are
metadata outside ``labels``; no token, payload, or free text is recorded. An
invalid field omits the record with a fixed warning, and nothing here can fail
the release: every path returns zero.

The inputs arrive as environment variables:

- ``RELEASE_OPERATION``: the phase, one of :data:`OPERATIONS`.
- ``RELEASE_STEPS``: whitespace-separated ``category=outcome`` pairs in step
  order, each naming the failure category a failed step stands for and that
  step's ``steps.<id>.outcome``. The last pair is the phase's action.
- ``RELEASE_JOB_STATUS``: ``job.status`` when the record is written.
- ``RELEASE_ATTEMPTS`` and ``RELEASE_HTTP_STATUS``: the PyPI index request's
  attempt count and final HTTP status, empty for phases without one.
- ``PHASE_STARTED_AT``: the job's start, in whole Unix seconds.

It uses the runner's preinstalled ``python3`` and the standard library only,
as ``scripts/release_assets.py`` does and for the same reason.

Examples
--------
>>> retry_bucket("3")
'1-2'
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

SCHEMA_VERSION = 1
METRIC = "release_phase_outcomes_total"
OPERATIONS = frozenset({
    "check_version",
    "attest",
    "draft_release",
    "publish_pypi",
    "github_upload",
    "publish_release",
})
FAILURE_CATEGORIES = frozenset({
    "none",
    "setup",
    "version_mismatch",
    "collection",
    "attestation",
    "index_http",
    "github_api",
    "upload",
    "digest_mismatch",
})
OUTCOMES = frozenset({"success", "failure", "skipped"})
RETRY_BUCKETS = ("0", "1-2", "3+")
ELAPSED_BUCKETS = ("under_1m", "1m_5m", "5m_15m", "over_15m", "unknown")
HTTP_STATUS_CLASSES = frozenset({"none", "network", "2xx", "3xx", "4xx", "5xx"})

#: Upper bounds, in seconds, of every elapsed band but the open-ended last.
_ELAPSED_BOUNDS = ((60, "under_1m"), (300, "1m_5m"), (900, "5m_15m"))
_MANY_RETRIES = 3
_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+[0-9A-Za-z.+-]{0,32}")
_HTTP_STATUS = re.compile(r"[0-9]{3}")
_WARNING = "::warning title=release-telemetry::"


def _is_decimal(value: str) -> bool:
    """Return whether ``value`` is a non-empty run of ASCII digits."""
    return value.isascii() and value.isdecimal()


def retry_bucket(attempts: str) -> str:
    """Band the retries behind ``attempts`` requests into a closed label.

    Parameters
    ----------
    attempts : str
        The request count as a decimal string; empty when nothing was sent.

    Returns
    -------
    str
        ``0`` for a first-time success or no request, ``1-2``, or ``3+``.
    """
    retries = int(attempts) - 1 if _is_decimal(attempts) else 0
    if retries <= 0:
        return RETRY_BUCKETS[0]
    return RETRY_BUCKETS[1] if retries < _MANY_RETRIES else RETRY_BUCKETS[2]


def http_status_class(status: str) -> str:
    """Classify a ``curl`` ``%{http_code}``; ``000`` means no response."""
    if not _HTTP_STATUS.fullmatch(status):
        return "none"
    if status == "000":
        return "network"
    label = f"{status[0]}xx"
    return label if label in HTTP_STATUS_CLASSES else "none"


def elapsed_bucket(started_at: str, now: float) -> str:
    """Band the phase's elapsed time; ``unknown`` without a valid start."""
    if not _is_decimal(started_at) or int(started_at) > now:
        return ELAPSED_BUCKETS[-1]
    elapsed = now - int(started_at)
    return next(
        (label for bound, label in _ELAPSED_BOUNDS if elapsed < bound), "over_15m"
    )


def _pairs(steps: str) -> list[tuple[str, str]]:
    """Parse ``category=outcome`` pairs; raise on any unknown category."""
    split = [item.partition("=") for item in steps.split()]
    pairs = [(category, result) for category, _, result in split]
    if not pairs or any(category not in FAILURE_CATEGORIES for category, _ in pairs):
        msg = "unknown failure category"
        raise ValueError(msg)
    return pairs


def outcome_of(steps: str, job_status: str) -> tuple[str, str]:
    """Derive the phase's outcome and failure category from its steps.

    Parameters
    ----------
    steps : str
        Whitespace-separated ``category=outcome`` pairs, the action last.
    job_status : str
        ``job.status`` when the record is written.

    Returns
    -------
    tuple[str, str]
        The first failed step's category; ``setup`` when the job failed
        outside the listed steps; ``skipped`` when the action did not run.
    """
    pairs = _pairs(steps)
    failed = next((category for category, result in pairs if result == "failure"), None)
    if failed is not None:
        return "failure", failed
    if job_status == "failure":
        return "failure", "setup"
    if pairs[-1][1] == "skipped":
        return "skipped", "none"
    return "success", "none"


def build_record(environ: cabc.Mapping[str, str], now: float) -> dict[str, object]:
    """Build one bounded record from the step environment.

    Parameters
    ----------
    environ : collections.abc.Mapping[str, str]
        The step environment described in the module docstring.
    now : float
        The current Unix time.

    Returns
    -------
    dict[str, object]
        The schema-version-1 record, ready to encode as one JSON line.

    Raises
    ------
    ValueError
        If any label or metadata value falls outside its closed form.
    """
    operation = environ.get("RELEASE_OPERATION", "")
    identity: dict[str, str] = {
        "run_id": environ.get("GITHUB_RUN_ID", ""),
        "run_attempt": environ.get("GITHUB_RUN_ATTEMPT", ""),
    }
    tag = environ.get("GITHUB_REF_NAME", "")
    if operation not in OPERATIONS or not all(map(_is_decimal, identity.values())):
        msg = "invalid operation or run identity"
        raise ValueError(msg)
    if not _TAG.fullmatch(tag):
        msg = "invalid tag"
        raise ValueError(msg)
    outcome, category = outcome_of(
        environ.get("RELEASE_STEPS", ""), environ.get("RELEASE_JOB_STATUS", "")
    )
    labels = {
        "operation": operation,
        "outcome": outcome,
        "failure_category": category,
        "retry_bucket": retry_bucket(environ.get("RELEASE_ATTEMPTS", "")),
        "elapsed_bucket": elapsed_bucket(environ.get("PHASE_STARTED_AT", ""), now),
        "http_status_class": http_status_class(environ.get("RELEASE_HTTP_STATUS", "")),
    }
    recorded_at = dt.datetime.fromtimestamp(now, dt.UTC).isoformat(timespec="seconds")
    return {
        "schema_version": SCHEMA_VERSION,
        "metric": METRIC,
        "value": 1,
        "labels": labels,
        **identity,
        "tag": tag,
        "recorded_at": recorded_at.replace("+00:00", "Z"),
    }


def persist(record: dict[str, object], environ: cabc.Mapping[str, str]) -> None:
    """Append ``record`` to the job's JSONL file and advertise it."""
    encoded = json.dumps(record, separators=(",", ":"))
    directory = Path(environ["RUNNER_TEMP"]) / "release-telemetry"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "records.jsonl").open("a", encoding="utf-8") as records:
        records.write(encoded + "\n")
    with Path(environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write("written=true\n")


def main(environ: cabc.Mapping[str, str], now: float) -> int:
    """Write one record, or warn with a fixed message; always return zero."""
    try:
        record = build_record(environ, now)
    except ValueError:
        print(f"{_WARNING}Invalid record fields; record omitted.")
        return 0
    try:
        persist(record, environ)
    except (OSError, KeyError):
        print(f"{_WARNING}Could not persist the release record.")
    return 0


if __name__ == "__main__":
    sys.exit(main(os.environ, time.time()))
