"""Compare two pytest layouts offline, and check that they ran the same suite.

Issue #607 turns on two questions that must be answered from recorded evidence
rather than from a feeling about a log line: did the candidate run the same
tests, and was it actually cheaper on the whole job? This module answers both
from files that a CI run already produced.

This module owns the records and the decision rule. Loading them from JSON and
comparing collected node IDs live in `scripts/pytest_layout_analysis`, so each
module stays under the repository's size limit without splitting one claim
across two files.

``compare`` pairs control and candidate runs and reports the paired saving. It
is deliberately conservative:

- it refuses to pair runs from different interpreters, because the suites are
  not comparable across them;
- it refuses to pair a warm run with a cold one, because the difference is the
  cache rather than the layout;
- it reports ``inconclusive`` rather than a median when fewer pairs survive;
- it counts failed, cancelled and retried work in the cost, so a candidate
  cannot look cheaper by failing sooner.

Nothing here talks to the network or to GitHub. Fetching job and step timestamps
is a separate, explicit step, and the inputs are plain JSON so a reviewer can
re-derive every number in the report.
"""

from __future__ import annotations

import statistics
import typing as typ

from scripts.pytest_layout_records import (
    SCHEMA_VERSION,
    AdoptionThresholds,
    Comparison,
    Pair,
    RunRecord,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "SCHEMA_VERSION",
    "AdoptionThresholds",
    "Comparison",
    "Pair",
    "RunRecord",
    "compare",
    "pair_runs",
)

#: Fewer pairs than this cannot support a median, so the report says
#: `inconclusive` rather than quoting a number nobody should act on.
MINIMUM_PAIRS = 5


def pair_runs(
    records: cabc.Sequence[RunRecord],
) -> tuple[tuple[Pair, ...], tuple[RunRecord, ...]]:
    """Pair each control run with at most one candidate run.

    Pairing is deliberately strict: two runs are comparable only when they share
    an interpreter and a cache-state class. Pooling across interpreters would
    compare different suites; pooling across cache states would compare a warm
    build with a cold one and attribute the difference to the layout.

    Parameters
    ----------
    records : Sequence of RunRecord
        Every run available.

    Returns
    -------
    tuple
        ``(pairs, unpaired)``. A run with no partner is returned in
        ``unpaired`` rather than dropped, so a sparse sample stays visible.
    """
    controls = [run for run in records if run.layout == "baseline"]
    candidates = [run for run in records if run.layout == "consolidated"]
    used: set[str] = set()
    pairs: list[Pair] = []
    unpaired: list[RunRecord] = []
    for control in controls:
        partner = next(
            (
                run
                for run in candidates
                if run.run_id not in used
                and run.python_version == control.python_version
                and run.cache_state == control.cache_state
            ),
            None,
        )
        if partner is None:
            unpaired.append(control)
            continue
        used.add(partner.run_id)
        pairs.append(Pair(control=control, candidate=partner))
    unpaired.extend(run for run in candidates if run.run_id not in used)
    return tuple(pairs), tuple(unpaired)


def compare(
    records: cabc.Sequence[RunRecord],
    thresholds: AdoptionThresholds | None = None,
    *,
    minimum_pairs: int = MINIMUM_PAIRS,
) -> Comparison:
    """Judge a candidate layout against its control.

    Parameters
    ----------
    records : Sequence of RunRecord
        Every run available.
    thresholds : AdoptionThresholds or None
        The predeclared decision rule. Defaults to the rule this plan froze.
    minimum_pairs : int
        Pairs required before a median is quoted.

    Returns
    -------
    Comparison
        The pairs, the unpaired runs, the median saving and a bounded state.

    Notes
    -----
    The rule is applied in one order, and the first clause that fires decides:

    1. too few pairs, or no pair with both step times, is ``inconclusive``;
    2. any pair regressing beyond the tolerance is ``rejected``;
    3. a median below either threshold is ``rejected``;
    4. otherwise the candidate is ``adopted``.

    A failed or cancelled run does not change the median, but it does mark its
    pair as counted in the cost, so a candidate that failed its way to a short
    step time cannot be read as a saving.
    """
    rule = AdoptionThresholds() if thresholds is None else thresholds
    pairs, unpaired = pair_runs(records)
    verdict = _decide(pairs, rule, minimum_pairs)
    return Comparison(
        pairs=pairs,
        unpaired=unpaired,
        state=verdict.state,
        reason=verdict.reason,
        median_saving_seconds=verdict.median_seconds,
        median_saving_fraction=verdict.median_fraction,
    )


class _Verdict(typ.NamedTuple):
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


def _decide(
    pairs: cabc.Sequence[Pair],
    rule: AdoptionThresholds,
    minimum_pairs: int,
) -> _Verdict:
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
    _Verdict
        The state, its reason, and the medians it rests on.
    """
    seconds, fraction = _medians(pairs)
    if len(pairs) < minimum_pairs:
        return _Verdict(
            "inconclusive",
            f"{len(pairs)} comparable pairs is fewer than the {minimum_pairs} "
            "needed for a median; report the sample rather than a threshold",
            seconds,
            fraction,
        )
    if seconds is None or fraction is None:
        return _Verdict(
            "inconclusive",
            "no pair recorded both step times, so no saving can be computed",
            seconds,
            fraction,
        )
    rejected = _regression_reason(pairs, rule) or _minimum_reason(
        seconds, fraction, rule
    )
    if rejected is not None:
        return _Verdict("rejected", rejected, seconds, fraction)
    return _Verdict(
        "adopted",
        f"median saving {seconds:.1f}s ({fraction:.1%}) clears the predeclared "
        "minimum with no pair beyond the regression tolerance",
        seconds,
        fraction,
    )


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
