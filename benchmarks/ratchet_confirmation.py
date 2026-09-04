"""Pure policy for confirming benchmark-ratchet regressions."""

from __future__ import annotations

from benchmarks.ratchet_types import ConfirmationReport, ConfirmationResult


def confirm_regressions(
    *, primary: ConfirmationReport, confirmation: ConfirmationReport
) -> ConfirmationResult:
    """Intersect a primary verdict with usable confirmation evidence.

    Parameters
    ----------
    primary : ConfirmationReport
        Typed primary measurement whose regressions may be narrowed.
    confirmation : ConfirmationReport
        Typed retry measurement. Missing comparison evidence preserves every
        primary regression rather than treating the retry as a pass.

    Returns
    -------
    ConfirmationResult
        Primary, confirmed, and unconfirmed regression values. Confirmation-
        only scenarios cannot enter the result.
    """
    primary_regressions = primary.regressions
    if confirmation.comparison_performed:
        confirmed_names = {entry.scenario_name for entry in confirmation.regressions}
        confirmed = tuple(
            entry
            for entry in primary_regressions
            if entry.scenario_name in confirmed_names
        )
    else:
        confirmed = primary_regressions
    confirmed_names = {entry.scenario_name for entry in confirmed}
    unconfirmed = tuple(
        entry
        for entry in primary_regressions
        if entry.scenario_name not in confirmed_names
    )
    return ConfirmationResult(
        primary_regressions=primary_regressions,
        confirmed_regressions=confirmed,
        unconfirmed_regressions=unconfirmed,
    )
