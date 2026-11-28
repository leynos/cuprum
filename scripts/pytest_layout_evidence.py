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

import typing as typ

from scripts.pytest_layout_decision import MINIMUM_PAIRS, decide
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

#: Re-exported so a caller reading a comparison names one module for the rule
#: and the record it applies to.
__all__ += ("MINIMUM_PAIRS",)


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
        partner = _first_comparable(control, candidates, used)
        if partner is None:
            unpaired.append(control)
            continue
        used.add(partner.run_id)
        pairs.append(Pair(control=control, candidate=partner))
    unpaired.extend(run for run in candidates if run.run_id not in used)
    return tuple(pairs), tuple(unpaired)


def _is_comparable(control: RunRecord, candidate: RunRecord) -> bool:
    """Return whether two runs may be compared at all.

    Two runs are comparable only when they share an interpreter and a
    cache-state class. Pooling across interpreters would compare different
    suites; pooling across cache states would compare a warm build with a cold
    one and attribute the difference to the layout.

    Returns
    -------
    bool
        Whether the pair shares an interpreter and a cache-state class.
    """
    return (
        candidate.python_version == control.python_version
        and candidate.cache_state == control.cache_state
    )


def _first_comparable(
    control: RunRecord,
    candidates: cabc.Sequence[RunRecord],
    used: cabc.Set[str],
) -> RunRecord | None:
    """Return the first unused candidate comparable to ``control``.

    Parameters
    ----------
    control : RunRecord
        The baseline run seeking a partner.
    candidates : Sequence of RunRecord
        The candidate runs, in input order.
    used : set of str
        Run IDs already paired, so no candidate is used twice.

    Returns
    -------
    RunRecord or None
        The partner, or ``None`` when no unused candidate is comparable.
    """
    for candidate in candidates:
        if candidate.run_id not in used and _is_comparable(control, candidate):
            return candidate
    return None


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
    verdict = decide(pairs, rule, minimum_pairs, unpaired)
    return Comparison(
        pairs=pairs,
        unpaired=unpaired,
        state=verdict.state,
        reason=verdict.reason,
        median_saving_seconds=verdict.median_seconds,
        median_saving_fraction=verdict.median_fraction,
    )
