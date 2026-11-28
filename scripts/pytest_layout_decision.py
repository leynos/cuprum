"""The predeclared rule that decides whether a layout change is adopted.

The arithmetic lives here rather than beside the pairing code so that each
module stays under the repository's size and complexity limits, and so the rule
a reviewer checks is separable from the search that produced its input.

The rule is fixed before any candidate runs, and its clauses fire in a declared
order: too little admissible evidence is ``inconclusive``; a pair beyond the
regression tolerance is ``rejected``; a median below either predeclared minimum
is ``rejected``; otherwise the candidate is ``adopted``. `_usable` defines what
counts as evidence, and it is deliberately strict, because a rule that admitted
a cold pair or a failed run would report adoption on a sample the plan never
accepted.
"""

from __future__ import annotations

import statistics
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from scripts.pytest_layout_records import AdoptionThresholds, Pair, RunRecord

__all__ = ("MINIMUM_PAIRS", "Verdict", "decide")

#: Fewer pairs than this cannot support a median, so the report says
#: `inconclusive` rather than quoting a number nobody should act on.
MINIMUM_PAIRS = 5


class Verdict(typ.NamedTuple):
    """One decision, with the medians it was reached from.

    Attributes
    ----------
    state : str
        ``adopted``, ``rejected`` or ``inconclusive``.
    reason : str
        Why the state was reached, in words a reviewer can check.
    median_seconds : float or None
        Median paired saving, or ``None`` when none could be computed.
    median_fraction : float or None
        Median paired saving as a fraction of the control step.
    """

    state: str
    reason: str
    median_seconds: float | None
    median_fraction: float | None


def decide(
    pairs: cabc.Sequence[Pair],
    rule: AdoptionThresholds,
    minimum_pairs: int,
) -> Verdict:
    """Apply the predeclared rule to a paired sample.

    The clauses run in a fixed order and the first to fire decides, so one
    sample always yields one verdict. Each clause delegates its arithmetic, so
    this function reads as the rule itself.

    Parameters
    ----------
    pairs : Sequence of Pair
        Comparable control/candidate pairs.
    rule : AdoptionThresholds
        The predeclared decision rule.
    minimum_pairs : int
        Pairs required before a median is quoted.

    Returns
    -------
    Verdict
        The state, its reason, and the medians it rests on.
    """
    usable = _usable(pairs)
    seconds, fraction = _medians(usable)
    if len(usable) < minimum_pairs:
        return Verdict(
            "inconclusive",
            f"{len(usable)} usable pairs is fewer than the {minimum_pairs} "
            "needed for a median; a pair counts only when both sides are warm "
            "and neither failed, and report the sample rather than a threshold",
            seconds,
            fraction,
        )
    if seconds is None or fraction is None:
        return Verdict(
            "inconclusive",
            "no pair recorded both step times, so no saving can be computed",
            seconds,
            fraction,
        )
    gap = _evidence_gap(usable, minimum_pairs)
    if gap is not None:
        return Verdict("inconclusive", gap, seconds, fraction)
    rejected = _regression_reason(usable, rule) or _minimum_reason(
        seconds, fraction, rule
    )
    if rejected is not None:
        return Verdict("rejected", rejected, seconds, fraction)
    return Verdict(
        "adopted",
        f"median saving {seconds:.1f}s ({fraction:.1%}) clears the predeclared "
        "minimum with no pair beyond the regression tolerance",
        seconds,
        fraction,
    )


def _usable(pairs: cabc.Sequence[Pair]) -> tuple[Pair, ...]:
    """Return the pairs the predeclared rule may draw a median from.

    A pair counts only when both runs are warm and neither failed. A cold run
    measures the cache rather than the layout, and a failed run's short step
    time is not a saving; admitting either would let the tool report adoption
    on evidence the plan never accepted.

    Parameters
    ----------
    pairs : Sequence of Pair
        Comparable control/candidate pairs.

    Returns
    -------
    tuple of Pair
        The pairs whose saving is admissible.
    """
    return tuple(
        pair
        for pair in pairs
        if pair.control.cache_state == "warm"
        and pair.candidate.cache_state == "warm"
        and not pair.control.failed
        and not pair.candidate.failed
        and pair.saving_seconds is not None
    )


def _evidence_gap(pairs: cabc.Sequence[Pair], minimum_pairs: int) -> str | None:
    """Return what the sample still lacks, or ``None`` when it is complete.

    A median is evidence only when the sample is the one the plan predeclared:
    enough warm pairs per interpreter, on comparable runners and toolchains,
    over runs that collected the same suite, at no extra whole-job cost. Each
    condition is asked in turn and the first unmet one is reported, so a
    reviewer learns the next thing to fix rather than a list.

    Parameters
    ----------
    pairs : Sequence of Pair
        The pairs already admitted as usable.
    minimum_pairs : int
        Pairs required per interpreter and in total.

    Returns
    -------
    str or None
        The reason the sample is incomplete, or ``None`` when it is complete.
    """
    for reason in (
        _thin_leg_reason(pairs, minimum_pairs),
        _fingerprint_reason(pairs),
        _collection_reason(pairs),
        _cost_reason(pairs),
    ):
        if reason is not None:
            return reason
    return None


def _thin_leg_reason(pairs: cabc.Sequence[Pair], minimum_pairs: int) -> str | None:
    """Return why an interpreter leg is under-sampled, or ``None``."""
    counts: dict[str, int] = {}
    for pair in pairs:
        leg = pair.control.python_version
        counts[leg] = counts.get(leg, 0) + 1
    thin = {leg: count for leg, count in counts.items() if count < minimum_pairs}
    if not thin:
        return None
    return (
        f"these interpreter legs have fewer than {minimum_pairs} warm pairs: "
        f"{thin}; a median per leg is what the plan requires"
    )


def _fingerprint_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why a pair ran on an incomparable runner, or ``None``."""
    mismatched = [
        pair.control.run_id
        for pair in pairs
        if _fingerprints_differ(pair.control, pair.candidate)
    ]
    if not mismatched:
        return None
    return f"these pairs compare different runners or toolchains: {mismatched}"


def _collection_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why a pair collected a different suite, or ``None``."""
    divergent = [
        pair.control.run_id
        for pair in pairs
        if _collections_differ(pair.control, pair.candidate)
    ]
    if not divergent:
        return None
    return f"these pairs collected different suites: {divergent}"


def _cost_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why the arm's whole-job cost rose, or ``None``."""
    if _cost_is_non_positive(pairs):
        return None
    return "the candidate's whole-job seconds exceed the control's"


def _fingerprints_differ(control: RunRecord, candidate: RunRecord) -> bool:
    """Return whether two runs disagree on a recorded comparability key."""
    for field_name in ("runner_label", "toolchain"):
        left = getattr(control, field_name)
        right = getattr(candidate, field_name)
        if left != right:
            return True
    return False


def _collections_differ(control: RunRecord, candidate: RunRecord) -> bool:
    """Return whether two runs collected different node multisets."""
    return sorted(control.node_ids) != sorted(candidate.node_ids)


def _cost_is_non_positive(pairs: cabc.Sequence[Pair]) -> bool:
    """Return whether the candidate's whole-job seconds do not exceed the control's.

    A pair without both job times cannot supply the comparison, so it is
    skipped rather than counted as free.

    Returns
    -------
    bool
        Whether the candidate's total whole-job seconds are not greater.
    """
    usable = [
        pair
        for pair in pairs
        if pair.control.job_seconds is not None
        and pair.candidate.job_seconds is not None
    ]
    if not usable:
        return True
    control_total = sum(pair.control.job_seconds or 0.0 for pair in usable)
    candidate_total = sum(pair.candidate.job_seconds or 0.0 for pair in usable)
    return candidate_total <= control_total


def _medians(pairs: cabc.Sequence[Pair]) -> tuple[float | None, float | None]:
    """Return the median paired saving in seconds and as a fraction."""
    savings = [p.saving_seconds for p in pairs if p.saving_seconds is not None]
    fractions = [p.saving_fraction for p in pairs if p.saving_fraction is not None]
    return (
        statistics.median(savings) if savings else None,
        statistics.median(fractions) if fractions else None,
    )


def _regression_reason(
    pairs: cabc.Sequence[Pair], rule: AdoptionThresholds
) -> str | None:
    """Return why a large regression rejects the arm, or ``None``.

    One pair beyond the tolerance rejects the arm rather than being averaged
    away: an occasionally much slower suite is a real cost even when the median
    looks good.

    Returns
    -------
    str or None
        The rejection text, or ``None`` when no pair exceeds the tolerance.
    """
    regressions = [
        pair
        for pair in pairs
        if pair.saving_seconds is not None
        and pair.saving_seconds < -rule.maximum_pair_regression_seconds
    ]
    if not regressions:
        return None
    slowest = min(pair.saving_seconds or 0.0 for pair in regressions)
    return (
        f"{len(regressions)} pair(s) regressed by more than "
        f"{rule.maximum_pair_regression_seconds}s; the slowest is {slowest:.1f}s"
    )


def _minimum_reason(
    seconds: float, fraction: float, rule: AdoptionThresholds
) -> str | None:
    """Return why the median is not useful, or ``None`` when it clears both."""
    if seconds >= rule.minimum_saving_seconds and (
        fraction >= rule.minimum_saving_fraction
    ):
        return None
    return (
        f"median saving {seconds:.1f}s ({fraction:.1%}) is below the "
        f"{rule.minimum_saving_seconds:.0f}s and "
        f"{rule.minimum_saving_fraction:.1%} predeclared minimum"
    )
