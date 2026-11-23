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
>>> _worth_it(45.0, 12.0, 40.0)
True
>>> _worth_it(5.0, 5.0, 1.0)
True
>>> _worth_it(None, 5.0, 1.0) is None
True
"""

from __future__ import annotations

import math
import typing as typ

from scripts import compiler_cache_cost as cost_reader
from scripts import compiler_cache_pairs as pairs_reader
from scripts import compiler_cache_records as records_reader
from scripts import compiler_cache_schema as schema
from scripts import compiler_cache_validation as validation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

PAIRS_REQUIRED = 5
MINIMUM_FRACTION = 0.05
_SCOPE_MESSAGE = (
    "incremental_cost_seconds is family-scoped; a multi-family summary "
    "cannot attribute one value"
)


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


class ConsoleFacts(typ.NamedTuple):
    """The console terms the gate consumes."""

    verified: bool
    fidelity_matched: bool
    incremental: float | None


def _console_facts(console: cabc.Mapping[str, object] | None) -> ConsoleFacts:
    """Read the console's gate terms as reported, without promoting them."""
    return ConsoleFacts(
        verified=console is not None and console.get("billing_rounding") == "verified",
        fidelity_matched=(
            console is not None and console.get("test_fidelity") == "matched"
        ),
        incremental=cost_reader.console_number(
            console, "incremental_cost_seconds", signed=True
        ),
    )


def _adoption(facts: ConsoleFacts, *, protected: bool) -> Adoption:
    """Return the gate's adoption terms; an unverified cost is not evidence."""
    return Adoption(
        fidelity_matched=facts.fidelity_matched,
        protected=protected,
        incremental=facts.incremental if facts.verified else None,
    )


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
    """Evaluate the gate for one family.

    Returns
    -------
    dict[str, object]
        The family's saving, its five per cent floor, the transfer it
        replaces, the console's incremental cost and break-even consumers, and
        the verdict.
    """
    measured = _measurements(family)
    facts = _console_facts(console)
    enough = int(typ.cast("int", family["pairs"])) >= PAIRS_REQUIRED
    worth_it = _worth_it(measured.saving, measured.floor, measured.transfer)
    break_even = (
        _break_even(
            cost_reader.console_number(console, "initial_population_seconds"),
            measured.transfer,
            measured.saving,
        )
        if facts.verified
        else None
    )
    adoption = _adoption(facts, protected=protected)
    return {
        "family": family["family"],
        "pairs": family["pairs"],
        "median_saving_seconds": measured.saving,
        "control_occupancy_seconds": measured.occupancy,
        "five_percent_floor_seconds": measured.floor,
        "save_restore_seconds": measured.transfer,
        "minimum_worthwhile_seconds": measured.minimum,
        "incremental_cost_seconds": facts.incremental,
        "break_even_consumers": break_even,
        "branch_protection": "enabled" if protected else "unknown",
        "verdict": _verdict(enough=enough, worth_it=worth_it, adoption=adoption),
    }


def _check_console_scope(
    families: cabc.Mapping[str, object],
    console: cabc.Mapping[str, object] | None,
) -> None:
    """Refuse a console cost that cannot be attributed to one family."""
    has_cost = (
        console is not None and console.get("incremental_cost_seconds") is not None
    )
    if len(families) > 1 and has_cost:
        raise records_reader.InvalidEvidenceError(_SCOPE_MESSAGE)


def build_summary(
    records: cabc.Sequence[dict[str, object]],
    jobs: cabc.Sequence[cabc.Mapping[str, object]],
    names: cabc.Mapping[str, str],
    console: cabc.Mapping[str, object] | None,
) -> dict[str, object]:
    """Compute the experiment summary, leaving unsupported fields ``unknown``."""
    grouped = pairs_reader.pairs_for(records)
    every = pairs_reader.records_by_family(records)
    _check_console_scope(every, console)
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
        "cost": cost_reader.cost(records, jobs, names, console),
        "gate": _gate(families, console),
    }
