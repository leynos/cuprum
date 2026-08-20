"""Decide whether a reported regression survives a second measurement.

The window in `benchmarks/ratchet_history.py` makes the *bar* robust to one
noisy run. It cannot make the *candidate* robust: a pull request measured
once on an unlucky runner still reports whatever that runner produced, and
the only recourse is a human deciding to press re-run.

So when the first comparison reports a regression, CI measures again and
compares again, and this module intersects the two verdicts. A scenario
fails only if it regressed both times. A flake has to land on the same
scenario twice to survive, which turns a one-in-N false failure into
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
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib as pth
import typing as typ

from benchmarks._validation import _require_list, _require_mapping

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_logger = logging.getLogger(__name__)


def _regressed_scenarios(report: cabc.Mapping[str, object]) -> list[str]:
    """Return the scenario names a ratchet report flagged as regressions."""
    entries = _require_list(report.get("regressions", []), name="regressions")
    return [
        str(_require_mapping(entry, name="regressions[]").get("scenario_name"))
        for entry in entries
    ]


def _comparison_performed(report: cabc.Mapping[str, object]) -> bool:
    """Return whether a ratchet report actually compared anything.

    A skip report — no baseline yet, or an incompatible benchmark profile —
    records `comparison_performed: False` and carries no comparisons.
    """
    return report.get("comparison_performed") is not False


def confirm_regressions(
    *,
    primary: cabc.Mapping[str, object],
    confirmation: cabc.Mapping[str, object],
) -> dict[str, object]:
    """Combine two ratchet reports into one verdict.

    The result keeps the primary report's shape, so the workflow summary and
    every other consumer read it unchanged, with `regressions` narrowed to
    those the second measurement reproduced.
    """
    flagged = _regressed_scenarios(primary)
    if not _comparison_performed(confirmation):
        _logger.warning(
            "confirmation run did not compare; keeping the primary verdict for "
            "%d flagged scenario(s)",
            len(flagged),
        )
        reproduced = set(flagged)
    else:
        reproduced = set(flagged) & set(_regressed_scenarios(confirmation))

    entries = _require_list(primary.get("regressions", []), name="regressions")
    confirmed = [
        entry
        for entry in entries
        if _require_mapping(entry, name="regressions[]").get("scenario_name")
        in reproduced
    ]
    unconfirmed = [entry for entry in entries if entry not in confirmed]

    combined = dict(primary)
    combined.update({
        "passed": not confirmed,
        "confirmation_performed": True,
        "confirmed_regressions": confirmed,
        "unconfirmed_regressions": unconfirmed,
        "regressions": confirmed,
        "primary_regressions": entries,
        "confirmation_comparisons": confirmation.get("comparisons", []),
    })
    return combined


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
    """Write the combined verdict and return the process exit code."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        combined = confirm_regressions(
            primary=_load_report(args.primary_report),
            confirmation=_load_report(args.confirmation_report),
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(combined, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        _logger.error("failed to combine ratchet reports: %s", exc)  # noqa: TRY400
        return 2

    unconfirmed = combined["unconfirmed_regressions"]
    if unconfirmed:
        _logger.info(
            "%d regression(s) did not reproduce on a second measurement and are "
            "treated as runner noise",
            len(typ.cast("list[object]", unconfirmed)),
        )
    if combined["passed"]:
        return 0

    _logger.error(
        "benchmark ratchet failed: %d regression(s) reproduced on a second measurement",
        len(typ.cast("list[object]", combined["confirmed_regressions"])),
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
