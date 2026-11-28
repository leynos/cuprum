"""Pair compiler-cache observations into the experiment's families.

A pair is one control and one candidate of the same family and matrix leg.
Pairing is positional within each family, ordered by the record's timestamp;
only usable, warm observations take part: a degraded, non-compiling, or
un-reused record is cost evidence, not saving evidence.

Examples
--------
>>> pair_key({"family": "py3.13-debug", "matrix_leg": "none"})
'py3.13-debug/none'
>>> median([1.0, 3.0, 2.0])
2.0
"""

from __future__ import annotations

import math
import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_validation as validation
from scripts.compiler_cache_observations import cache_hits, median, pair_key

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _usable(record: cabc.Mapping[str, object]) -> bool:
    """Report whether a record may count as success evidence."""
    return record["state"] == "ok" and record["zero_requests"] == "nonzero"


def _warm(record: cabc.Mapping[str, object]) -> bool:
    """Report whether a consumer actually reused compiler objects."""
    hits = cache_hits(record)
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
        match record["backend"]:
            case "local":
                arm = "control"
            case "gha":
                arm = "candidate"
            case _:
                continue
        grouped.setdefault(pair_key(record), {"control": [], "candidate": []})[
            arm
        ].append(dict(record))
    return grouped


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
    total = restore + save
    # Two finite durations can still sum to infinity; a non-finite total is
    # unknown, and must not reach the JSON report as `Infinity`.
    return round(total, 3) if math.isfinite(total) else None


def _comparable(
    control: cabc.Mapping[str, object], candidate: cabc.Mapping[str, object]
) -> bool:
    """Report whether two observations describe the same compiler and shape.

    A pair whose runner class, toolchain, interpreter, or compiler identity
    differs measures that difference, not the cache backend, so it is excluded
    from the saving sample.

    Returns
    -------
    bool
        Whether every provenance field is identified and equal.
    """
    fields = (
        "runner_class",
        "rustc",
        "toolchain",
        "compiler_shape",
        "python_version",
    )
    for field in fields:
        if not _known_equal(control.get(field), candidate.get(field)):
            return False
    return True


def _known_equal(left: object, right: object) -> bool:
    """Report whether two provenance values are identified and equal."""
    if isinstance(left, dict) or isinstance(right, dict):
        return _mapping_equal(left, right)
    return _known(left) and left == right


def _mapping_equal(left: object, right: object) -> bool:
    """Compare two provenance mappings, ignoring an all-unknown side."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if not left or set(left) != set(right):
        return False
    known_left = {key: value for key, value in left.items() if _known(value)}
    known_right = {key: value for key, value in right.items() if _known(value)}
    return bool(known_left) and known_left == known_right


def _known(value: object) -> bool:
    """Report whether a provenance scalar identifies something."""
    return value is not None and value != "unknown"


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


def pair_evidence(
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
