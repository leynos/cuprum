"""Compute each compiler-cache family's paired saving and failures.

Pairing itself belongs to ``scripts.compiler_cache_pairs``; this module turns
one family's pair evidence into the review artefact's statistics and counts the
distinct failed API jobs across all of the family's records.
"""

from __future__ import annotations

import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts.compiler_cache_observations import median
from scripts.compiler_cache_pairs import JoinContext, pair_evidence

if typ.TYPE_CHECKING:
    import collections.abc as cabc


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


def saving_aggregate(
    savings: cabc.Sequence[float],
    controls: cabc.Sequence[float],
    candidates: cabc.Sequence[float],
) -> dict[str, object]:
    """Return the medians, extremes, and the raw saving sample."""
    return {
        "median_control_seconds": median(controls),
        "median_candidate_seconds": median(candidates),
        "median_saving_seconds": median(savings),
        "slowest_saving_seconds": min(savings) if savings else None,
        "fastest_saving_seconds": max(savings) if savings else None,
        "savings_seconds": savings,
    }


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
        The family's counts, medians, extremes, failures, and transfer time.
    """
    evidence = pair_evidence(arms, context.jobs, context.names)
    savings = typ.cast("list[float]", evidence["savings"])
    summary = {
        "family": family,
        "control_observations": len(arms["control"]),
        "candidate_observations": len(arms["candidate"]),
        "pairs": len(savings),
        "independent_job_pairs": evidence["job_pairs"],
        "incomparable_pairs": evidence["incomparable"],
        "failures": failure_count(all_records, context.jobs, context.names),
    }
    summary.update(
        saving_aggregate(
            savings,
            typ.cast("list[float]", evidence["controls"]),
            typ.cast("list[float]", evidence["candidates"]),
        )
    )
    summary["median_transfer_seconds"] = evidence["transfer"]
    return summary
