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
from scripts import compiler_cache_validation as validation

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

    A valid pull request's first run is registered before the cold and
    degraded branches, so a cold first run does not let a later warm run
    become ``first-pr-consumer``. The first run keeps its cold or degraded
    label; every later run of that PR is a subsequent push.

    Returns
    -------
    str
        One of the five closed observation classes.
    """
    pr_number = str(record["pr_number"])
    is_pr = record["event_class"] == "pull_request" and pr_number.isdecimal()
    first_run = None
    if is_pr:
        first_run = first_for_pr.setdefault(pr_number, str(record["run_id"]))
    hits = typ.cast("dict[str, object]", record["counters"]).get("cache_hits")
    cold = (
        record["backend"] == "local" and record["restore_hit"] is not True and not hits
    )
    if record["state"] != "ok":
        return "fallback-or-misconfiguration"
    if cold:
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


def records_by_family(
    records: cabc.Sequence[cabc.Mapping[str, object]],
) -> dict[str, list[cabc.Mapping[str, object]]]:
    """Group every record by family and leg, whether or not it can pair."""
    grouped: dict[str, list[cabc.Mapping[str, object]]] = {}
    for record in records:
        grouped.setdefault(pair_key(record), []).append(record)
    return grouped


def failure_count(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> int:
    """Count the distinct API jobs among ``records`` that did not pass.

    Every record of the family is considered, including degraded fallbacks
    and jobs that are not warm enough to pair: a compiler-only fallback must
    not hide a failing job from the failure count.

    Returns
    -------
    int
        The number of distinct failed jobs among the family's records.
    """
    failed: set[tuple[str, str, str, str]] = set()
    for record in records:
        result = jobs_reader.seconds_and_result(record, jobs, names)[1]
        if result != "failed":
            continue
        failed.add((
            str(record["run_id"]),
            str(record["run_attempt"]),
            str(record["job"]),
            str(record["matrix_leg"]),
        ))
    return len(failed)


def _unique_jobs(
    records: cabc.Sequence[cabc.Mapping[str, object]],
) -> list[cabc.Mapping[str, object]]:
    """Collapse a bracket list to one observation per job and matrix leg."""
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[cabc.Mapping[str, object]] = []
    for record in records:
        key = (
            str(record["run_id"]),
            str(record["run_attempt"]),
            str(record["job"]),
            str(record["matrix_leg"]),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def _transfer_total(record: cabc.Mapping[str, object]) -> float | None:
    """Return one job's restore-plus-save seconds, or ``None`` if incomplete."""
    restore = validation.number_or_none(record.get("restore_seconds"))
    save = validation.number_or_none(record.get("save_seconds"))
    if restore is None or save is None:
        return None
    return round(restore + save, 3)


def _comparable(
    control: cabc.Mapping[str, object], candidate: cabc.Mapping[str, object]
) -> bool:
    """Report whether two observations describe the same compiler and shape.

    A pair whose toolchain, interpreter, or compiler identity differs measures
    the change between them, not the cache backend, so it is excluded from the
    saving sample.

    Returns
    -------
    bool
        Whether every provenance field matches.
    """
    fields = ("rustc", "toolchain", "compiler_shape", "python_version")
    return all(control.get(field) == candidate.get(field) for field in fields)


def _paired_savings(
    controls: cabc.Sequence[cabc.Mapping[str, object]],
    candidates: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, object]:
    """Return the usable pairs, the failures, and the independent pair count."""
    count = min(len(controls), len(candidates))
    savings: list[float] = []
    control_times: list[float] = []
    candidate_times: list[float] = []
    incomparable = 0
    for index in range(count):
        control_time, control_result = jobs_reader.seconds_and_result(
            controls[index], jobs, names
        )
        candidate_time, candidate_result = jobs_reader.seconds_and_result(
            candidates[index], jobs, names
        )
        if not _comparable(controls[index], candidates[index]):
            incomparable += 1
            continue
        if control_result != "passed" or candidate_result != "passed":
            continue
        if control_time is None or candidate_time is None:
            continue
        control_times.append(control_time)
        candidate_times.append(candidate_time)
        savings.append(round(control_time - candidate_time, 3))
    return {
        "savings": savings,
        "controls": control_times,
        "candidates": candidate_times,
        "incomparable": incomparable,
        "job_pairs": count,
    }


def _pair_evidence(
    arms: cabc.Mapping[str, list[dict[str, object]]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, object]:
    """Return one family's independent job pairs, failures, and transfer time."""
    controls = _unique_jobs(arms["control"])
    candidates = _unique_jobs(arms["candidate"])
    evidence = _paired_savings(controls, candidates, jobs, names)
    transfers = [
        total for record in controls if (total := _transfer_total(record)) is not None
    ]
    evidence["transfer"] = median(transfers)
    return evidence


class JoinContext(typ.NamedTuple):
    """The jobs document and the declared job-name mapping."""

    jobs: cabc.Sequence[cabc.Mapping[str, object]]
    names: cabc.Mapping[str, str]


def family_summary(
    family: str,
    arms: cabc.Mapping[str, list[dict[str, object]]],
    context: JoinContext,
    all_records: cabc.Sequence[cabc.Mapping[str, object]],
) -> dict[str, object]:
    """Compute one family's paired saving and its failure evidence.

    Returns
    -------
    dict[str, object]
        The family's observation counts, pair count, median and extreme
        savings, failures, and median restore-plus-save time.
    """
    evidence = _pair_evidence(arms, context.jobs, context.names)
    savings = typ.cast("list[float]", evidence["savings"])
    return {
        "family": family,
        "control_observations": len(arms["control"]),
        "candidate_observations": len(arms["candidate"]),
        "pairs": len(savings),
        "independent_job_pairs": evidence["job_pairs"],
        "incomparable_pairs": evidence["incomparable"],
        "failures": failure_count(all_records, context.jobs, context.names),
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
