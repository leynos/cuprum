"""Classify and group compiler-cache observations.

The experiment's observation classes are decided from each record: a degraded
record is a fallback or misconfiguration, an unwarmed control is a cold or new
family, a pull request's first run is its first consumer, and later runs are
subsequent pushes. Grouping by family/leg identity lives here too, so the
pairing module only decides eligibility and provenance.
"""

from __future__ import annotations

import typing as typ

from scripts import compiler_cache_jobs as jobs_reader

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def median(values: cabc.Sequence[float]) -> float | None:
    """Return the median of ``values``, or ``None`` when there are none."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[middle], 3)
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


def pair_key(record: cabc.Mapping[str, object]) -> str:
    """Return the family/leg identity a pair must share."""
    return f"{record['family']}/{record['matrix_leg']}"


def cache_hits(record: cabc.Mapping[str, object]) -> object:
    """Return a record's cache-hit count, whatever the server reported."""
    return typ.cast("dict[str, object]", record["counters"]).get("cache_hits")


def register_first_run(
    record: cabc.Mapping[str, object], first_for_pr: dict[str, str]
) -> str | None:
    """Record a pull request's first run and return it, or ``None`` off a PR.

    Registration happens before the cold and degraded branches, so a cold
    first run does not let a later warm run become ``first-pr-consumer``.

    Returns
    -------
    str | None
        The PR's first run id, or ``None`` when the record is not a pull
        request with a decimal number.
    """
    pr_number = str(record["pr_number"])
    if record["event_class"] != "pull_request" or not pr_number.isdecimal():
        return None
    return first_for_pr.setdefault(pr_number, str(record["run_id"]))


def cold_control(record: cabc.Mapping[str, object]) -> bool:
    """Report whether a local archive consumer restored nothing usable."""
    return (
        record["backend"] == "local"
        and record["restore_hit"] is not True
        and not cache_hits(record)
    )


def observation_label(
    record: cabc.Mapping[str, object], first_for_pr: dict[str, str]
) -> str:
    """Return the experiment case one record witnesses.

    The first run of a pull request keeps its cold or degraded label; every
    later run of that PR is a subsequent push.

    Returns
    -------
    str
        One of the five closed observation classes.
    """
    first_run = register_first_run(record, first_for_pr)
    if record["state"] != "ok":
        return "fallback-or-misconfiguration"
    if cold_control(record):
        return "cold-or-new-family"
    if first_run is None:
        return "fresh-consumer"
    if first_run == str(record["run_id"]):
        return "first-pr-consumer"
    return "subsequent-pr-push"


def classify_observations(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> list[dict[str, object]]:
    """Label each record with the experiment case it witnesses.

    Each case is decided from the record, not a run's position in a list: a
    degraded record is a fallback, an unwarmed control is cold, the first
    record of a pull request is its first consumer, and later runs are pushes.

    Returns
    -------
    list[dict[str, object]]
        One entry per record with its identity, class, and job result.
    """
    first_for_pr: dict[str, str] = {}
    classified: list[dict[str, object]] = []
    for record in sorted(records, key=lambda item: str(item["recorded_at"])):
        label = observation_label(record, first_for_pr)
        classified.append({
            "run_id": record["run_id"],
            "run_attempt": record["run_attempt"],
            "job": record["job"],
            "bracket": record["bracket"],
            "observation_class": label,
            "job_result": jobs_reader.seconds_and_result(record, jobs, names)[1],
        })
    return classified


def records_by_family(
    records: cabc.Sequence[cabc.Mapping[str, object]],
) -> dict[str, list[cabc.Mapping[str, object]]]:
    """Group every record by family and leg, whether or not it can pair."""
    grouped: dict[str, list[cabc.Mapping[str, object]]] = {}
    for record in records:
        grouped.setdefault(pair_key(record), []).append(record)
    return grouped
