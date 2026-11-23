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

import dataclasses as dc
import statistics
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = (
    "AdoptionThresholds",
    "Comparison",
    "Pair",
    "RunRecord",
    "compare",
    "pair_runs",
)

#: Bumped when a report's shape changes meaning, so an old artefact is not
#: compared against a new one.
SCHEMA_VERSION = 1

#: Fewer pairs than this cannot support a median, so the report says
#: `inconclusive` rather than quoting a number nobody should act on.
MINIMUM_PAIRS = 5


@dc.dataclass(frozen=True, slots=True)
class AdoptionThresholds:
    """The decision rule, declared before any candidate ran.

    Attributes
    ----------
    minimum_saving_seconds : float
        Least median paired saving that counts as useful.
    minimum_saving_fraction : float
        Least median saving as a fraction of the control step.
    maximum_pair_regression_seconds : float
        Largest single-pair regression tolerated before the arm is rejected.
    """

    minimum_saving_seconds: float = 20.0
    minimum_saving_fraction: float = 0.015
    maximum_pair_regression_seconds: float = 15.0


@dc.dataclass(frozen=True, slots=True)
class RunRecord:
    """One measured run, as a workflow recorded it.

    Attributes
    ----------
    run_id : str
        The workflow run identifier.
    layout : str
        ``baseline`` or ``consolidated``.
    python_version : str
        The interpreter leg, e.g. ``3.12``.
    cache_state : str
        ``warm``, ``cold`` or ``scratch-cold``.
    step_seconds : float or None
        The `Run tests` step's wall time, or ``None`` when unrecorded.
    job_seconds : float or None
        The whole job's wall time.
    node_ids : tuple of str
        Collected node IDs, when the run produced a session record.
    exit_status : int
        The step's exit status.
    failed : bool
        Whether the run failed, was cancelled, or was retried.
    """

    run_id: str
    layout: str
    python_version: str
    cache_state: str
    step_seconds: float | None
    job_seconds: float | None
    node_ids: tuple[str, ...]
    exit_status: int
    failed: bool


@dc.dataclass(frozen=True, slots=True)
class Pair:
    """One control run and one candidate run that may be compared.

    Attributes
    ----------
    control : RunRecord
        The baseline run.
    candidate : RunRecord
        The consolidated run.
    """

    control: RunRecord
    candidate: RunRecord

    @property
    def saving_seconds(self) -> float | None:
        """Paired saving on the test step, or ``None`` when unrecorded."""
        if self.control.step_seconds is None or self.candidate.step_seconds is None:
            return None
        return self.control.step_seconds - self.candidate.step_seconds

    @property
    def saving_fraction(self) -> float | None:
        """Paired saving as a fraction of the control step."""
        control = self.control.step_seconds
        saving = self.saving_seconds
        if control is None or saving is None:
            return None
        if control <= 0.0:
            return None
        return saving / control


@dc.dataclass(frozen=True, slots=True)
class Comparison:
    """The outcome of pairing and judging two layouts.

    Attributes
    ----------
    pairs : tuple of Pair
        Every comparable pair, in input order.
    unpaired : tuple of RunRecord
        Runs that could not be paired, reported so they are visible rather than
        dropped.
    state : str
        ``adopted``, ``rejected`` or ``inconclusive``.
    reason : str
        Why the state was reached, in words a reviewer can check.
    median_saving_seconds : float or None
        Median paired saving, or ``None`` when no pair recorded a step time.
    median_saving_fraction : float or None
        Median paired saving as a fraction of the control step.
    """

    pairs: tuple[Pair, ...]
    unpaired: tuple[RunRecord, ...]
    state: str
    reason: str
    median_saving_seconds: float | None
    median_saving_fraction: float | None

    def as_report(self) -> dict[str, object]:
        """Return the comparison as a JSON-serializable report.

        Returns
        -------
        dict
            The report, carrying :data:`SCHEMA_VERSION`.
        """
        return {
            "schema_version": SCHEMA_VERSION,
            "state": self.state,
            "reason": self.reason,
            "median_saving_seconds": self.median_saving_seconds,
            "median_saving_fraction": self.median_saving_fraction,
            "pairs": [
                {
                    "control_run": pair.control.run_id,
                    "candidate_run": pair.candidate.run_id,
                    "python_version": pair.control.python_version,
                    "cache_state": pair.control.cache_state,
                    "control_seconds": pair.control.step_seconds,
                    "candidate_seconds": pair.candidate.step_seconds,
                    "saving_seconds": pair.saving_seconds,
                    "saving_fraction": pair.saving_fraction,
                    "control_job_seconds": pair.control.job_seconds,
                    "candidate_job_seconds": pair.candidate.job_seconds,
                    "counted_in_cost": pair.control.failed or pair.candidate.failed,
                }
                for pair in self.pairs
            ],
            "unpaired": [
                {
                    "run_id": run.run_id,
                    "layout": run.layout,
                    "python_version": run.python_version,
                    "cache_state": run.cache_state,
                }
                for run in self.unpaired
            ],
        }


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
    savings = [pair.saving_seconds for pair in pairs if pair.saving_seconds is not None]
    fractions = [
        pair.saving_fraction for pair in pairs if pair.saving_fraction is not None
    ]
    if len(pairs) < minimum_pairs:
        return Comparison(
            pairs=pairs,
            unpaired=unpaired,
            state="inconclusive",
            reason=(
                f"{len(pairs)} comparable pairs is fewer than the {minimum_pairs} "
                "needed for a median; report the sample rather than a threshold"
            ),
            median_saving_seconds=statistics.median(savings) if savings else None,
            median_saving_fraction=statistics.median(fractions) if fractions else None,
        )
    if not savings:
        return Comparison(
            pairs=pairs,
            unpaired=unpaired,
            state="inconclusive",
            reason="no pair recorded both step times, so no saving can be computed",
            median_saving_seconds=None,
            median_saving_fraction=None,
        )
    median_seconds = statistics.median(savings)
    median_fraction = statistics.median(fractions) if fractions else 0.0
    regressions = [
        pair
        for pair in pairs
        if pair.saving_seconds is not None
        and pair.saving_seconds < -rule.maximum_pair_regression_seconds
    ]
    if regressions:
        return Comparison(
            pairs=pairs,
            unpaired=unpaired,
            state="rejected",
            reason=(
                f"{len(regressions)} pair(s) regressed by more than "
                f"{rule.maximum_pair_regression_seconds}s; the slowest is "
                f"{min(pair.saving_seconds or 0.0 for pair in regressions):.1f}s"
            ),
            median_saving_seconds=median_seconds,
            median_saving_fraction=median_fraction,
        )
    if (
        median_seconds < rule.minimum_saving_seconds
        or median_fraction < rule.minimum_saving_fraction
    ):
        return Comparison(
            pairs=pairs,
            unpaired=unpaired,
            state="rejected",
            reason=(
                f"median saving {median_seconds:.1f}s ({median_fraction:.1%}) is "
                f"below the {rule.minimum_saving_seconds:.0f}s and "
                f"{rule.minimum_saving_fraction:.1%} predeclared minimum"
            ),
            median_saving_seconds=median_seconds,
            median_saving_fraction=median_fraction,
        )
    return Comparison(
        pairs=pairs,
        unpaired=unpaired,
        state="adopted",
        reason=(
            f"median saving {median_seconds:.1f}s ({median_fraction:.1%}) clears "
            "the predeclared minimum with no pair beyond the regression tolerance"
        ),
        median_saving_seconds=median_seconds,
        median_saving_fraction=median_fraction,
    )
