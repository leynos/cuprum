"""Select comparable benchmark-ratchet baseline evidence."""

from __future__ import annotations

import logging

from benchmarks.benchmark_profile import validate_matching_profiles
from benchmarks.ratchet_history import BaselineHistory
from benchmarks.ratchet_ratios import profile_metadata, run_ratios
from benchmarks.ratchet_types import (
    BaselineReason,
    BaselineSource,
    BenchmarkRunPayload,
    ComparisonState,
    RatchetDecision,
)

_logger = logging.getLogger(__name__)

__all__ = ["baseline_window", "compatible_history_window"]


def baseline_window(
    *,
    baseline: BenchmarkRunPayload | None,
    candidate: BenchmarkRunPayload,
    history: BaselineHistory | None,
    window_size: int,
) -> tuple[dict[str, tuple[float, ...]], RatchetDecision]:
    """Return baseline ratios and their durable selection decision.

    Parameters
    ----------
    baseline : BenchmarkRunPayload | None
        Single-sample fallback to use when no compatible history is available.
    candidate : BenchmarkRunPayload
        Candidate benchmark payload whose profile selects compatible history.
    history : BaselineHistory | None
        Main-branch samples to consider before using ``baseline``.
    window_size : int
        Maximum number of recent compatible samples to use.

    Returns
    -------
    tuple[dict[str, tuple[float, ...]], RatchetDecision]
        Baseline ratios grouped by comparison identifier and the durable
        decision describing the selected evidence source.

    Raises
    ------
    ValueError
        If neither compatible history nor a fallback baseline is available.
    """
    recent = compatible_history_window(
        candidate=candidate, history=history, window_size=window_size
    )
    if recent.samples:
        return (
            {name: recent.ratios_for(name) for name in recent.scenarios},
            RatchetDecision(
                baseline_source=BaselineSource.HISTORY,
                baseline_reason=BaselineReason.COMPATIBLE_HISTORY,
                compatible_sample_count=len(recent.samples),
                comparison_state=ComparisonState.COMPARED,
            ),
        )

    if baseline is None:
        msg = "a baseline run or a non-empty baseline history is required"
        raise ValueError(msg)
    _logger.info("no compatible baseline history; comparing against one sample")
    validate_matching_profiles(
        baseline_plan=baseline.plan, candidate_plan=candidate.plan
    )
    return (
        {name: (ratio,) for name, ratio in run_ratios(baseline).items()},
        RatchetDecision(
            baseline_source=BaselineSource.FALLBACK,
            baseline_reason=(
                BaselineReason.HISTORY_UNAVAILABLE
                if history is None
                else BaselineReason.NO_COMPATIBLE_HISTORY
            ),
            compatible_sample_count=0,
            comparison_state=ComparisonState.COMPARED,
        ),
    )


def compatible_history_window(
    *,
    candidate: BenchmarkRunPayload,
    history: BaselineHistory | None,
    window_size: int,
) -> BaselineHistory:
    """Return recent history samples compatible with the candidate's profile.

    Parameters
    ----------
    candidate : BenchmarkRunPayload
        Candidate benchmark payload whose profile metadata is required.
    history : BaselineHistory | None
        Main-branch history to filter, or ``None`` when no history was found.
    window_size : int
        Number of most recent compatible samples to retain.

    Returns
    -------
    BaselineHistory
        The compatible history suffix, or an empty history when ``history`` is
        ``None``.
    """
    if history is None:
        return BaselineHistory()
    version, worker_iterations = profile_metadata(candidate.plan)
    compatible = history.compatible_with(
        benchmark_profile_version=version,
        worker_iterations=worker_iterations,
    )
    return BaselineHistory(samples=compatible.samples[-window_size:])
