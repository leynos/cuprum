"""Decide whether a reported regression survives a second measurement.

The window in `benchmarks/ratchet_history.py` makes the *bar* robust to one
noisy run. It cannot make the *candidate* robust: a pull request measured
once on an unlucky runner still reports whatever that runner produced, and
the only recourse is a human deciding to press re-run.

So when the first comparison reports a regression, CI measures again and
compares again. `benchmarks.ratchet_confirmation` intersects typed evidence:
a scenario fails only if it regressed both times. A flake has to land on the
same scenario twice to survive, which turns a one-in-N false failure into
roughly one-in-N².

Two asymmetries are deliberate:

- Confirmation can only turn a failure into a pass. A scenario the first
  run did not flag is not failed by the second, however it measured —
  otherwise re-measuring would add a second chance to fail, doubling the
  false-failure rate it exists to reduce.
- A confirmation run that could not compare at all leaves the first
  verdict standing. Failing closed is right here: the primary comparison
  succeeded on the same inputs, so an unusable confirmation is a fault in
  the retry, not evidence about the candidate.

This module is the JSON and command-line adapter around that policy. It
decodes persisted reports, writes the established combined-report shape, and
maps successful, regressed, and invalid-input outcomes to process statuses.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import logging
import pathlib as pth
import typing as typ

from benchmarks._validation import (
    _require_list,
    _require_mapping,
    _require_non_empty_string,
)
from benchmarks.ratchet_confirmation import confirm_regressions, confirmation_status
from benchmarks.ratchet_types import (
    ConfirmationReport,
    ConfirmationResult,
    ReportedRegression,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_logger = logging.getLogger(__name__)


@dc.dataclass(frozen=True, slots=True)
class _DecodedConfirmationReport:
    """Validated JSON adapter values for one confirmation report."""

    report: ConfirmationReport
    payload: dict[str, object]
    regression_entries: tuple[dict[str, object], ...]


def _comparison_performed(payload: cabc.Mapping[str, object]) -> bool:
    """Return whether a ratchet report actually compared anything.

    A skip report — no baseline yet, or an incompatible benchmark profile —
    records `comparison_performed: False` and carries no comparisons.

    Returns
    -------
    bool
        Whether the report contains a comparable measurement.
    """
    if payload.get("comparison_performed") is False:
        return False
    comparisons = payload.get("comparisons")
    return isinstance(comparisons, list) and bool(comparisons)


def _regression_entries(
    payload: cabc.Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    """Validate and copy the serialized regression entries."""
    entries = _require_list(payload.get("regressions"), name="regressions")
    return tuple(
        dict(_require_mapping(entry, name="regressions[]")) for entry in entries
    )


def _decoded_report_from_payload(
    payload: cabc.Mapping[str, object], *, require_regressions: bool
) -> _DecodedConfirmationReport:
    """Decode JSON-shaped report data into typed confirmation evidence."""
    performed = _comparison_performed(payload)
    if not require_regressions and not performed:
        entries: tuple[dict[str, object], ...] = ()
    else:
        entries = _regression_entries(payload)
    regressions = tuple(
        ReportedRegression(
            scenario_name=_require_non_empty_string(
                entry.get("scenario_name"), name="regressions[].scenario_name"
            )
        )
        for entry in entries
    )
    return _DecodedConfirmationReport(
        report=ConfirmationReport(
            regressions=regressions,
            comparison_performed=performed,
        ),
        payload=dict(payload),
        regression_entries=entries,
    )


def confirmation_report_from_payload(
    payload: cabc.Mapping[str, object],
) -> ConfirmationReport:
    """Decode a complete ratchet report into typed confirmation evidence.

    Parameters
    ----------
    payload : collections.abc.Mapping[str, object]
        JSON-shaped ratchet report containing a `regressions` list.

    Returns
    -------
    ConfirmationReport
        Validated regression names and whether comparison evidence exists.

    Raises
    ------
    TypeError, ValueError
        If the report does not contain valid regression entries.
    """  # ruff: ignore[docstring-extraneous-exception] - validation errors are part of the adapter contract.
    return _decoded_report_from_payload(payload, require_regressions=True).report


def confirmation_result_to_payload(
    *,
    primary_payload: cabc.Mapping[str, object],
    confirmation_payload: cabc.Mapping[str, object],
    result: ConfirmationResult,
    confirmation: ConfirmationReport,
) -> dict[str, object]:
    """Adapt a typed confirmation result to the established JSON report.

    Parameters
    ----------
    primary_payload : collections.abc.Mapping[str, object]
        Original primary report whose fields and regression entries are kept.
    confirmation_payload : collections.abc.Mapping[str, object]
        Original confirmation report supplying serialized comparison evidence.
    result : ConfirmationResult
        Pure confirmation-policy decision to serialize.
    confirmation : ConfirmationReport
        Typed retry evidence used to derive the bounded confirmation status.

    Returns
    -------
    dict[str, object]
        Existing combined report shape consumed by workflow-report readers.
    """
    primary = _decoded_report_from_payload(primary_payload, require_regressions=True)
    return _confirmation_result_to_payload(
        primary=primary,
        confirmation_payload=confirmation_payload,
        result=result,
        confirmation=confirmation,
    )


def _confirmation_result_to_payload(
    *,
    primary: _DecodedConfirmationReport,
    confirmation_payload: cabc.Mapping[str, object],
    result: ConfirmationResult,
    confirmation: ConfirmationReport,
) -> dict[str, object]:
    """Serialize a confirmation result from an already-decoded primary report."""
    confirmed_names = {entry.scenario_name for entry in result.confirmed_regressions}
    confirmed = [
        entry
        for regression, entry in zip(
            primary.report.regressions, primary.regression_entries, strict=True
        )
        if regression.scenario_name in confirmed_names
    ]
    unconfirmed = [
        entry
        for regression, entry in zip(
            primary.report.regressions, primary.regression_entries, strict=True
        )
        if regression.scenario_name not in confirmed_names
    ]
    combined = dict(primary.payload)
    combined.update({
        "passed": result.passed,
        "confirmation_performed": True,
        "confirmation_status": confirmation_status(
            result=result, confirmation=confirmation
        ).value,
        "confirmed_regressions": confirmed,
        "unconfirmed_regressions": unconfirmed,
        "regressions": confirmed,
        "primary_regressions": list(primary.regression_entries),
        "confirmation_comparisons": confirmation_payload.get("comparisons", []),
    })
    return combined


def combine_report_payloads(
    *, primary: cabc.Mapping[str, object], confirmation: cabc.Mapping[str, object]
) -> dict[str, object]:
    """Decode, combine, and serialize two ratchet report payloads.

    Parameters
    ----------
    primary : collections.abc.Mapping[str, object]
        Serialized primary ratchet report with validated regression evidence.
    confirmation : collections.abc.Mapping[str, object]
        Serialized confirmation report, which may intentionally record no
        comparison evidence.

    Returns
    -------
    dict[str, object]
        Combined report containing the established `passed`, regression, and
        confirmation fields consumed by workflow-report readers.

    Raises
    ------
    TypeError, ValueError
        If either payload fails the ratchet-report validation contract.
    """  # ruff: ignore[docstring-extraneous-exception] - validation errors are part of the adapter contract.
    primary_report = _decoded_report_from_payload(primary, require_regressions=True)
    confirmation_report = _decoded_report_from_payload(
        confirmation, require_regressions=False
    )
    result = confirm_regressions(
        primary=primary_report.report,
        confirmation=confirmation_report.report,
    )
    return _confirmation_result_to_payload(
        primary=primary_report,
        confirmation_payload=confirmation_report.payload,
        result=result,
        confirmation=confirmation_report.report,
    )


def _load_report(path: pth.Path) -> dict[str, object]:
    """Load one ratchet report."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return dict(_require_mapping(payload, name=f"ratchet report from {path}"))


def _parse_args(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse command-line arguments for the confirmation CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-report", type=pth.Path, required=True)
    parser.add_argument("--confirmation-report", type=pth.Path, required=True)
    parser.add_argument("--output", type=pth.Path, required=True)
    return parser.parse_args(argv)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Write the combined verdict and return the process exit code.

    Parameters
    ----------
    argv : cabc.Sequence[str] | None
        Optional command-line arguments. The process arguments are used when
        this is `None`.

    Returns
    -------
    int
        `0` when no primary regression was reproduced, `1` when at least one
        was reproduced, or `2` when a report cannot be read or combined.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        combined = combine_report_payloads(
            primary=_load_report(args.primary_report),
            confirmation=_load_report(args.confirmation_report),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(combined, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        _logger.exception("failed to combine ratchet reports")
        return 2

    unconfirmed = _require_list(
        combined["unconfirmed_regressions"], name="unconfirmed_regressions"
    )
    confirmed = _require_list(
        combined["confirmed_regressions"], name="confirmed_regressions"
    )
    if unconfirmed:
        _logger.info(
            "%d regression(s) did not reproduce on a second measurement and are "
            "treated as runner noise",
            len(unconfirmed),
        )
    if combined["passed"]:
        return 0

    _logger.error(
        "benchmark ratchet failed: %d regression(s) reproduced on a second measurement",
        len(confirmed),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
