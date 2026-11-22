"""Pair compiler-cache observations into the experiment's families.

A pair is one control observation and one candidate observation of the same
family and matrix leg. The pairing is positional within each family, ordered
by the record's own timestamp, and only observations that are usable and warm
take part: a degraded, non-compiling, or un-reused record is cost evidence,
not saving evidence.

Examples
--------
>>> pair_key({"family": "py3.13-debug", "matrix_leg": "none"})
'py3.13-debug/none'
>>> median([1.0, 3.0, 2.0])
2.0
>>> median([]) is None
True
"""

from __future__ import annotations

import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_records as records_reader

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


def _usable(record: cabc.Mapping[str, object]) -> bool:
    """Report whether a record may count as success evidence."""
    return record["state"] == "ok" and record["zero_requests"] == "nonzero"


def _warm(record: cabc.Mapping[str, object]) -> bool:
    """Report whether a consumer actually reused compiler objects."""
    hits = typ.cast("dict[str, object]", record["counters"]).get("cache_hits")
    if record["backend"] == "local":
        return record["restore_hit"] is True and bool(hits)
    return bool(hits)


def pairs_for(
    records: cabc.Sequence[cabc.Mapping[str, object]],
) -> dict[str, dict[str, list[dict[str, object]]]]:
    """Group usable, warm records by family and arm, in recorded order."""
    grouped: dict[str, dict[str, list[dict[str, object]]]] = {}
    for record in records:
        if not _usable(record) or not _warm(record):
            continue
        arm = "candidate" if record["backend"] == "gha" else "control"
        grouped.setdefault(pair_key(record), {"control": [], "candidate": []})[
            arm
        ].append(dict(record))
    return grouped


def _observation_label(
    record: cabc.Mapping[str, object], first_for_pr: dict[str, str]
) -> str:
    """Return the experiment case one record witnesses.

    ``first_for_pr`` is updated in place when the record is a pull request's
    first observation, so a later record for the same PR is a subsequent push.

    Returns
    -------
    str
        One of the five closed observation classes.
    """
    pr_number = str(record["pr_number"])
    hits = typ.cast("dict[str, object]", record["counters"]).get("cache_hits")
    cold = (
        record["backend"] == "local" and record["restore_hit"] is not True and not hits
    )
    if record["state"] != "ok":
        return "fallback-or-misconfiguration"
    if cold:
        return "cold-or-new-family"
    if pr_number and pr_number not in first_for_pr:
        first_for_pr[pr_number] = str(record["run_id"])
        return "first-pr-consumer"
    if pr_number:
        return "subsequent-pr-push"
    return "fresh-consumer"


def classify_observations(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> list[dict[str, object]]:
    """Label each record with the experiment case it witnesses.

    The cases are the ones the issue requires, and each is decided from the
    record rather than from a run's position in a list: a degraded record is a
    fallback or misconfiguration, an unwarmed control is a cold or new family,
    the first record of a pull request is that PR's first consumer, and a later
    one is a subsequent push.

    Returns
    -------
    list[dict[str, object]]
        One entry per record, with its identity, ``observation_class``, and
        the API job's result where the jobs document allows a join.
    """
    first_for_pr: dict[str, str] = {}
    classified: list[dict[str, object]] = []
    for record in sorted(records, key=lambda item: str(item["recorded_at"])):
        label = _observation_label(record, first_for_pr)
        classified.append({
            "run_id": record["run_id"],
            "run_attempt": record["run_attempt"],
            "job": record["job"],
            "bracket": record["bracket"],
            "observation_class": label,
            "job_result": jobs_reader.seconds_and_result(record, jobs, names)[1],
        })
    return classified


def _pair_evidence(
    arms: cabc.Mapping[str, list[dict[str, object]]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, object]:
    """Return one family's paired savings, failure count, and transfer time."""
    count = min(len(arms["control"]), len(arms["candidate"]))
    savings: list[float] = []
    controls: list[float] = []
    candidates: list[float] = []
    failures = 0
    for index in range(count):
        control_seconds, control_result = jobs_reader.seconds_and_result(
            arms["control"][index], jobs, names
        )
        candidate_seconds, candidate_result = jobs_reader.seconds_and_result(
            arms["candidate"][index], jobs, names
        )
        failures += sum(
            result == "failed" for result in (control_result, candidate_result)
        )
        if control_seconds is None or candidate_seconds is None:
            continue
        controls.append(control_seconds)
        candidates.append(candidate_seconds)
        savings.append(round(control_seconds - candidate_seconds, 3))
    timings = [
        value
        for record in arms["control"]
        for name in ("restore_seconds", "save_seconds")
        if (value := records_reader.number_or_none(record.get(name))) is not None
    ]
    return {
        "savings": savings,
        "controls": controls,
        "candidates": candidates,
        "failures": failures,
        "transfer": median(timings),
    }


def family_summary(
    family: str,
    arms: cabc.Mapping[str, list[dict[str, object]]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, object]:
    """Compute one family's paired saving and its failure evidence.

    Returns
    -------
    dict[str, object]
        The family's observation counts, pair count, median and extreme
        savings, failures, and median restore-plus-save time.
    """
    evidence = _pair_evidence(arms, jobs, names)
    savings = typ.cast("list[float]", evidence["savings"])
    return {
        "family": family,
        "control_observations": len(arms["control"]),
        "candidate_observations": len(arms["candidate"]),
        "pairs": len(savings),
        "failures": evidence["failures"],
        "median_control_seconds": median(typ.cast("list[float]", evidence["controls"])),
        "median_candidate_seconds": median(
            typ.cast("list[float]", evidence["candidates"])
        ),
        "median_saving_seconds": median(savings),
        "slowest_saving_seconds": min(savings) if savings else None,
        "fastest_saving_seconds": max(savings) if savings else None,
        "savings_seconds": savings,
        "median_transfer_seconds": evidence["transfer"],
    }
