"""The preconditions a layout sample must satisfy before it can be judged.

A median is evidence only when the sample is the one the plan predeclared:
enough warm pairs per interpreter, on comparable runners and toolchains, over
runs that collected the same suite and reported the same results, at no extra
whole-job cost. This module is that checklist, kept apart from the decision
rule so each reads as one idea.

Every condition is asked in its own function and answers with a reason or
``None``, so a reviewer learns the next thing to fix rather than a list. Absent
evidence fails: a missing fingerprint, an empty collection or an unrecorded job
time is a difference, never an agreement.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from scripts.pytest_layout_records import Pair, RunRecord

__all__ = ("evidence_gap",)


def evidence_gap(
    pairs: cabc.Sequence[Pair],
    minimum_pairs: int,
    all_pairs: cabc.Sequence[Pair],
    unpaired: cabc.Sequence[RunRecord] = (),
) -> str | None:
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
    all_pairs : Sequence of Pair
        Every comparable pair, including failed and untimed ones. Cost is
        totalled over these, because failed work is still paid for.
    unpaired : Sequence of RunRecord
        Runs with no partner. Their job seconds are still paid for, so they
        count toward the cost comparison.

    Returns
    -------
    str or None
        The reason the sample is incomplete, or ``None`` when it is complete.
    """
    for reason in (
        _thin_leg_reason(pairs, minimum_pairs),
        _fingerprint_reason(pairs),
        _collection_reason(pairs),
        _outcome_evidence_reason(pairs),
        _cost_reason(all_pairs, unpaired),
    ):
        if reason is not None:
            return reason
    return None


def _report(offenders: object, template: str) -> str | None:
    """Return the refusal text for a non-empty offender set, or ``None``.

    Parameters
    ----------
    offenders : object
        The offenders to report. Any empty sequence or mapping is falsy.
    template : str
        Text with an ``{offenders}`` placeholder.

    Returns
    -------
    str or None
        The rendered reason, or ``None`` when there is nothing to report.
    """
    return template.format(offenders=offenders) if offenders else None


def _thin_leg_reason(pairs: cabc.Sequence[Pair], minimum_pairs: int) -> str | None:
    """Return why an interpreter leg is under-sampled, or ``None``."""
    counts: dict[str, int] = {}
    for pair in pairs:
        leg = pair.control.python_version
        counts[leg] = counts.get(leg, 0) + 1
    if len(counts) > 1:
        return (
            "the sample spans more than one interpreter leg "
            f"({sorted(counts)}); the plan requires a median per leg, and a "
            "pooled median would mix incomparable suites"
        )
    thin = {leg: count for leg, count in counts.items() if count < minimum_pairs}
    return _report(
        thin,
        f"these interpreter legs have fewer than {minimum_pairs} warm pairs: "
        "{offenders}; a median per leg is what the plan requires",
    )


def _offending_runs(
    pairs: cabc.Sequence[Pair],
    predicate: cabc.Callable[[RunRecord, RunRecord], bool],
) -> list[str]:
    """Return the control run IDs of pairs that fail ``predicate``.

    Parameters
    ----------
    pairs : Sequence of Pair
        The pairs to test.
    predicate : Callable
        Called with a control and a candidate run; true means they disagree.

    Returns
    -------
    list of str
        Control run IDs, so a reported pair names the run a reviewer can open.
    """
    return [
        pair.control.run_id for pair in pairs if predicate(pair.control, pair.candidate)
    ]


def _fingerprint_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why a pair ran on an incomparable runner, or ``None``."""
    return _report(
        _offending_runs(pairs, _fingerprints_differ),
        "these pairs compare different runners or toolchains: {offenders}",
    )


def _collection_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why a pair ran a different suite or reported different results.

    Collected names alone are not fidelity: a skip that became a pass, or an
    xfail that became a failure, is a change in the suite even when every name
    matches.

    Returns
    -------
    str or None
        The reason, or ``None`` when every pair agrees.
    """
    return _report(
        _offending_runs(pairs, _results_differ),
        "these pairs ran different suites or reported different results: {offenders}",
    )


def _outcome_evidence_reason(pairs: cabc.Sequence[Pair]) -> str | None:
    """Return why the sample lacks executed-outcome evidence, or ``None``.

    An absent outcome mapping is not an empty one. If neither side recorded
    outcomes for a pair that collected nodes, agreement between two empty
    mappings says nothing, so the sample cannot support a verdict.

    Returns
    -------
    str or None
        The reason, or ``None`` when every pair recorded outcomes.
    """
    missing = [
        pair.control.run_id
        for pair in pairs
        if pair.control.node_ids
        and not pair.control.outcomes
        and not pair.candidate.outcomes
    ]
    return _report(
        missing,
        "these pairs collected nodes but recorded no executed outcomes: {offenders}",
    )


def _cost_reason(
    pairs: cabc.Sequence[Pair], unpaired: cabc.Sequence[RunRecord] = ()
) -> str | None:
    """Return why the arm's whole-job cost rose, or ``None``.

    Unpaired runs are included: a run with no partner was still billed, and
    omitting it would let an expensive failure hide behind a tidy sample.

    Returns
    -------
    str or None
        The reason, or ``None`` when the candidate costs no more.
    """
    if _cost_is_non_positive(pairs, unpaired):
        return None
    return "the candidate's whole-job seconds exceed the control's"


def _fingerprints_differ(control: RunRecord, candidate: RunRecord) -> bool:
    """Return whether two runs disagree on a recorded comparability key."""
    for field_name in ("runner_label", "toolchain"):
        left = getattr(control, field_name)
        right = getattr(candidate, field_name)
        if left is None or right is None:
            return True
        if left != right:
            return True
    return False


def _results_differ(control: RunRecord, candidate: RunRecord) -> bool:
    """Return whether two runs differ in suite or in reported results.

    An empty collection on either side is a difference, not a match: the
    verdict must not treat "no evidence" as agreement.

    Returns
    -------
    bool
        Whether the pair differs in suite or in reported results.
    """
    if not control.node_ids or not candidate.node_ids:
        return True
    if sorted(control.node_ids) != sorted(candidate.node_ids):
        return True
    return (
        control.outcomes != candidate.outcomes
        or control.skip_reasons != candidate.skip_reasons
        or control.xfail_reasons != candidate.xfail_reasons
    )


def _cost_is_non_positive(
    pairs: cabc.Sequence[Pair], unpaired: cabc.Sequence[RunRecord] = ()
) -> bool:
    """Return whether the candidate's whole-job seconds do not exceed the control's.

    A pair without both job times cannot supply the comparison, so it is
    skipped rather than counted as free.

    Returns
    -------
    bool
        Whether the candidate's total whole-job seconds are not greater.
    """
    totals = _job_totals(pairs, unpaired)
    if totals is None:
        return False
    return totals[1] <= totals[0]


def _job_totals(
    pairs: cabc.Sequence[Pair],
    unpaired: cabc.Sequence[RunRecord] = (),
) -> tuple[float, float] | None:
    """Return the control and candidate whole-job seconds, or ``None``.

    ``None`` means at least one pair did not record both job times, which makes
    the cost comparison unavailable rather than free.

    Parameters
    ----------
    pairs : Sequence of Pair
        The pairs to total.

    Returns
    -------
    tuple of float or None
        ``(control_total, candidate_total)``, or ``None`` when incomplete.
    """
    if not _all_timed(pairs) or any(run.job_seconds is None for run in unpaired):
        return None
    control = sum(pair.control.job_seconds or 0.0 for pair in pairs)
    candidate = sum(pair.candidate.job_seconds or 0.0 for pair in pairs)
    control += _unpaired_total(unpaired, "baseline")
    candidate += _unpaired_total(unpaired, "consolidated")
    return (control, candidate)


def _unpaired_total(runs: cabc.Sequence[RunRecord], layout: str) -> float:
    """Return the whole-job seconds of the unpaired runs on one layout.

    Parameters
    ----------
    runs : Sequence of RunRecord
        The unpaired runs.
    layout : str
        ``baseline`` or ``consolidated``.

    Returns
    -------
    float
        Total job seconds attributed to that layout.
    """
    return sum(run.job_seconds or 0.0 for run in runs if run.layout == layout)


def _all_timed(pairs: cabc.Sequence[Pair]) -> bool:
    """Return whether every pair recorded both whole-job times.

    Parameters
    ----------
    pairs : Sequence of Pair
        The pairs to test.

    Returns
    -------
    bool
        Whether each pair carries both job times.
    """
    return all(
        pair.control.job_seconds is not None and pair.candidate.job_seconds is not None
        for pair in pairs
    )
