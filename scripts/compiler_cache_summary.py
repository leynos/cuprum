#!/usr/bin/env -S uv run python
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Turn compiler-cache records into the experiment's review artefact.

``scripts/compiler_cache_evidence.py`` writes one redacted JSON line per
bracketed compile step. This reader pairs control (archived directory) and
candidate (Actions backend) observations of the same family, joins them to
GitHub's jobs API for whole-job seconds, and evaluates the predeclared gate in
``docs/ci-cache-ownership.md``.

The reader never invents a number. A field it cannot supply is ``unknown``; a
counter an unavailable server did not report is ``null``. Records marked
``degraded`` or ``noncompliant`` are excluded from success evidence but kept in
the cost totals, because a fallback run still spends paid time.

Inputs
------
``--records``
    One or more JSONL files, or directories containing them.
``--jobs``
    A ``gh api .../jobs`` document, or a file holding a list of them.
``--job-names``
    A JSON object mapping a record's ``job`` key (``github.job``) to a jobs API
    display-name substring, optionally with a ``{leg}`` placeholder. The API
    does not expose the job key, so the mapping is declared rather than
    guessed.
``--console``
    A dated console-observation JSON: storage, eviction, branch protection, and
    billing rounding.

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

import math
import typing as typ

from scripts import compiler_cache_jobs as jobs_reader
from scripts import compiler_cache_pairs as pairs_reader
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_validation as validation

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

PAIRS_REQUIRED = 5
MINIMUM_FRACTION = 0.05


def _cost(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
    console: cabc.Mapping[str, object] | None,
) -> dict[str, object]:
    """Return paid job-seconds per workflow and the critical path.

    A job with several brackets is counted once: the records are deduplicated
    by workflow, run, attempt, job, leg, and bracket before the seconds are
    summed, so three typecheck brackets do not treble one job's cost.

    Returns
    -------
    dict[str, object]
        Paid seconds and critical path per workflow, the billing label, and
        the console's population, transfer, and incremental cost fields.
    """
    per_job: dict[tuple[str, str, str, str], float] = {}
    for record in records:
        job = jobs_reader.matching_job(record, jobs, names)
        if job is None:
            continue
        key = (
            str(record["workflow"]),
            str(record["run_id"]),
            str(record["run_attempt"]),
            f"{record['job']}/{record['matrix_leg']}",
        )
        seconds = jobs_reader.job_seconds(job)
        if seconds is not None:
            per_job[key] = seconds
    by_workflow: dict[str, float] = {}
    for (workflow, _run, _attempt, _key), seconds in per_job.items():
        by_workflow[workflow] = round(by_workflow.get(workflow, 0.0) + seconds, 3)
    critical = _critical_paths(records, jobs, names)
    verified = console is not None and console.get("billing_rounding") == "verified"
    return {
        "paid_job_seconds_by_workflow": by_workflow or None,
        "critical_path_seconds_by_workflow": critical or None,
        "billing": "verified" if verified else "proxy",
        "initial_population_seconds": _console_number(
            console, "initial_population_seconds"
        ),
        "incremental_transfer_seconds": _console_number(
            console, "incremental_transfer_seconds"
        ),
        "incremental_cost_seconds": _console_number(
            console, "incremental_cost_seconds", signed=True
        ),
    }


def _critical_paths(
    records: cabc.Sequence[cabc.Mapping[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
) -> dict[str, float]:
    """Return each workflow's longest run, measured from job timestamps.

    Returns
    -------
    dict[str, float]
        The longest run span per workflow, in seconds, or an empty mapping
        when no record could be joined to the jobs document.
    """
    spans: dict[tuple[str, str, str], list[dt.datetime]] = {}
    for record in records:
        job = jobs_reader.matching_job(record, jobs, names)
        if job is None:
            continue
        started = jobs_reader.instant(job.get("started_at"))
        completed = jobs_reader.instant(job.get("completed_at"))
        if started is None or completed is None:
            continue
        spans.setdefault(
            (
                str(record["workflow"]),
                str(record["run_id"]),
                str(record["run_attempt"]),
            ),
            [],
        ).extend([started, completed])
    critical: dict[str, float] = {}
    for (workflow, _run, _attempt), instants in spans.items():
        span = (max(instants) - min(instants)).total_seconds()
        critical[workflow] = round(max(critical.get(workflow, 0.0), span), 3)
    return critical


def _console_number(
    console: cabc.Mapping[str, object] | None, name: str, *, signed: bool = False
) -> float | None:
    """Return one finite numeric console field.

    Parameters
    ----------
    console : collections.abc.Mapping[str, object] | None
        The dated console observation, or ``None``.
    name : str
        The field to read.
    signed : bool
        Accept a negative value. Durations are non-negative; an incremental
        cost may be negative when the change saves money.

    Returns
    -------
    float | None
        The field, or ``None`` when the console is absent, the field is absent,
        or the value is not a finite number.
    """
    if console is None:
        return None
    value = console.get(name)
    if not validation.number(value):
        return None
    number = float(typ.cast("float", value))
    return number if signed or number >= 0 else None


def _gate(
    families: cabc.Sequence[cabc.Mapping[str, object]],
    console: cabc.Mapping[str, object] | None,
) -> dict[str, object]:
    """Evaluate the predeclared gate for every family with observations.

    A family is adopted only with at least five pairs, a median saving that
    beats the transfer it replaces and is at least five per cent of the
    control's whole-job time, matched test fidelity, branch protection still
    on, and non-positive incremental paid cost. Anything the console does not
    establish stays unknown, which cannot satisfy the gate.

    Returns
    -------
    dict[str, object]
        The required pair count and one decision per family with observations.
    """
    protected = console is not None and console.get("branch_protection") == "enabled"
    decisions = [_decision(family, console, protected=protected) for family in families]
    return {"pairs_required": PAIRS_REQUIRED, "decisions": decisions}


class Adoption(typ.NamedTuple):
    """The three conditions beyond a worthwhile, sufficient saving."""

    fidelity_matched: bool
    protected: bool
    incremental: float | None


def _verdict(*, enough: bool, worth_it: bool | None, adoption: Adoption) -> str:
    """Decide one family's verdict from its gate inputs.

    A missing threshold input is not evidence that the saving is
    insufficient, so it cannot produce ``reject``; only a known saving below
    a known threshold does.

    Returns
    -------
    str
        ``adopt`` when every gate term holds, ``reject`` when known evidence
        shows no worthwhile saving, and ``insufficient-evidence`` otherwise.
    """
    if not enough or worth_it is None:
        return "insufficient-evidence"
    if not worth_it:
        return "reject"
    unpaid = adoption.incremental is not None and adoption.incremental <= 0
    adoptable = adoption.fidelity_matched and adoption.protected
    if adoptable and unpaid:
        return "adopt"
    return "insufficient-evidence"


def _worth_it(
    saving: float | None, floor: float | None, transfer: float | None
) -> bool | None:
    """Report whether a saving clears both documented thresholds.

    The gate accepts a saving at least as large as the five per cent floor and
    strictly larger than the transfer it replaces, so the two are checked
    separately rather than against their maximum.

    Returns
    -------
    bool | None
        The comparison, or ``None`` when any input is unknown.
    """
    if saving is None or floor is None:
        return None
    if transfer is None:
        return None
    return saving >= floor and saving > transfer


def _minimum_worthwhile(floor: float | None, transfer: float | None) -> float | None:
    """Return the larger of the five per cent floor and the transfer it replaces.

    Returns
    -------
    float | None
        The saving a family must beat, or ``None`` when either input is
        unknown, which cannot satisfy the gate.
    """
    if floor is None or transfer is None:
        return None
    return max(floor, transfer)


def _break_even(
    population: float | None, transfer: float | None, saving: float | None
) -> int | None:
    """Return the consumers needed to repay the population and transfer.

    Returns
    -------
    int | None
        The ceiling of the cost divided by the per-consumer saving, or
        ``None`` when an input is unknown or the saving is not positive.
    """
    known = population is not None and transfer is not None and saving is not None
    if not known or saving <= 0:
        return None
    return math.ceil((population + transfer) / saving)


class Measurements(typ.NamedTuple):
    """The family-level values the gate compares."""

    saving: float | None
    occupancy: float | None
    transfer: float | None
    floor: float | None
    minimum: float | None


def _measurements(family: cabc.Mapping[str, object]) -> Measurements:
    """Read and derive one family's gate measurements.

    Returns
    -------
    Measurements
        The median saving, control occupancy, transfer, five per cent floor,
        and the larger threshold the family must beat.
    """
    saving = validation.number_or_none(family["median_saving_seconds"])
    occupancy = validation.number_or_none(family["median_control_seconds"])
    transfer = validation.number_or_none(family["median_transfer_seconds"])
    floor = round(MINIMUM_FRACTION * occupancy, 3) if occupancy is not None else None
    return Measurements(
        saving, occupancy, transfer, floor, _minimum_worthwhile(floor, transfer)
    )


def _decision(
    family: cabc.Mapping[str, object],
    console: cabc.Mapping[str, object] | None,
    *,
    protected: bool,
) -> dict[str, object]:
    """Evaluate the gate for one family."""
    """Evaluate the gate for one family.

    Returns
    -------
    dict[str, object]
        The family's saving, its five per cent floor, the transfer it
        replaces, the console's incremental cost and break-even consumers, and
        the verdict.
    """
    measured = _measurements(family)
    incremental = _console_number(console, "incremental_cost_seconds", signed=True)
    enough = int(typ.cast("int", family["pairs"])) >= PAIRS_REQUIRED
    worth_it = _worth_it(measured.saving, measured.floor, measured.transfer)
    verified = console is not None and console.get("billing_rounding") == "verified"
    break_even = (
        _break_even(
            _console_number(console, "initial_population_seconds"),
            measured.transfer,
            measured.saving,
        )
        if verified
        else None
    )
    adoption = Adoption(
        fidelity_matched=(
            console is not None and console.get("test_fidelity") == "matched"
        ),
        protected=protected,
        incremental=incremental,
    )
    return {
        "family": family["family"],
        "pairs": family["pairs"],
        "median_saving_seconds": measured.saving,
        "control_occupancy_seconds": measured.occupancy,
        "five_percent_floor_seconds": measured.floor,
        "save_restore_seconds": measured.transfer,
        "minimum_worthwhile_seconds": measured.minimum,
        "incremental_cost_seconds": incremental,
        "break_even_consumers": break_even,
        "branch_protection": "enabled" if protected else "unknown",
        "verdict": _verdict(enough=enough, worth_it=worth_it, adoption=adoption),
    }


def build_summary(
    records: cabc.Sequence[dict[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
    console: cabc.Mapping[str, object] | None,
) -> dict[str, object]:
    """Compute the experiment summary, leaving unsupported fields ``unknown``."""
    grouped = pairs_reader.pairs_for(records)
    every = pairs_reader.records_by_family(records)
    context = pairs_reader.JoinContext(jobs, names)
    families = [
        pairs_reader.family_summary(
            family,
            grouped.get(family, {"control": [], "candidate": []}),
            context,
            every.get(family, []),
        )
        for family in sorted(set(grouped) | set(every))
    ]
    return {
        "schema_version": schema.SCHEMA_VERSION,
        "generated_at": schema.now(),
        "records": {
            "total": len(records),
            "by_state": {
                state: sum(1 for record in records if record["state"] == state)
                for state in sorted(schema.STATES)
            },
            "degraded_or_noncompliant": sum(
                1 for record in records if record["state"] != "ok"
            ),
        },
        "observations": pairs_reader.classify_observations(records, jobs, names),
        "families": families,
        "cost": _cost(records, jobs, names, console),
        "gate": _gate(families, console),
    }
