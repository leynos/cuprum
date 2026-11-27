"""The records a pytest layout comparison is made of.

These four types are the wire shape of an evidence artefact: what one run
measured, what a control/candidate pair looks like, and what a finished
comparison reports. They live here rather than beside the pairing rules so
that each module stays under the repository's size limit, and so the shape a
reviewer reads is separable from the arithmetic that fills it.

`RunRecord` is loaded from JSON that a workflow produced, so its field types
are deliberately narrow: a value that cannot be interpreted as a number is
recorded as ``None`` rather than coerced, because a coerced zero would read as
a measurement.
"""

from __future__ import annotations

import dataclasses as dc

__all__ = ("SCHEMA_VERSION", "AdoptionThresholds", "Comparison", "Pair", "RunRecord")

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
