"""Measure the construction share of the per-line event-emission hot path.

Roadmap item 5.2.1 asks whether hoisting invariant execution metadata out of
the per-line callback brings the share of parent samples spent *constructing*
observe events under the configured limit of the per-line consume subtree.
This script is the gate that answers it, from one py-spy raw-stack capture.

The parsed shapes and the capture and rules parsers live in
:mod:`benchmarks._line_event_profile_model`; the classification and reporting
live in :mod:`benchmarks._line_event_profile_classifier`. Both modules' public
names are re-exported here so the gate has a single entry point, and
:mod:`benchmarks.summarize_folded` supplies the shared notion of what a counted
folded-stack line is.

Unresolved frames are reported rather than silently scored, and a run with no
denominator, no matched construction frames, or a malformed capture exits with
status 2 so a missing symbol can never read as a zero-cost result.

Exit status
-----------
0
    Valid capture with a construction share at or below the limit.
1
    Valid capture with a construction share above the limit.
2
    Malformed, insufficient, or unresolved input: the run is inconclusive and
    must not be read as a passing measurement.
"""

from __future__ import annotations

import argparse
import json
import pathlib as pth
import sys
import typing as typ

from benchmarks._line_event_profile_classifier import (
    CONSTRUCTION_SHARE_LIMIT_PERCENT,
    classify_capture,
    summarize,
)
from benchmarks._line_event_profile_model import (
    Capture,
    ClassificationRule,
    ClassifierRules,
    Frame,
    FramePattern,
    Stack,
    _ProfileInputError,
    load_rules,
    parse_capture,
    parse_frame,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_EXIT_PASS = 0
_EXIT_ABOVE_LIMIT = 1
_EXIT_INCONCLUSIVE = 2

# Re-exported so the gate keeps one import surface. The model and classifier
# modules own the definitions; listing them here documents the intended entry
# point for callers and tests, which import this module rather than either
# half. A test asserts every name here is the owner's own object, so a
# redefinition cannot creep in.
__all__ = [
    "CONSTRUCTION_SHARE_LIMIT_PERCENT",
    "Capture",
    "ClassificationRule",
    "ClassifierRules",
    "Frame",
    "FramePattern",
    "Stack",
    "classify_capture",
    "load_rules",
    "main",
    "parse_capture",
    "parse_frame",
    "summarize",
]


def _exit_status(result: dict[str, object]) -> int:
    """Map a result document's status onto the process exit status."""
    status = result["status"]
    if status == "pass":
        return _EXIT_PASS
    if status == "fail_above_limit":
        return _EXIT_ABOVE_LIMIT
    return _EXIT_INCONCLUSIVE


def _parse_args(argv: cabc.Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folded", type=pth.Path, help="py-spy raw stack capture")
    parser.add_argument(
        "--rules", type=pth.Path, required=True, help="classifier rule set JSON"
    )
    parser.add_argument(
        "--output", type=pth.Path, required=True, help="result JSON destination"
    )
    return parser.parse_args(argv)


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Classify one capture and write the result document.

    Returns
    -------
    int
        The process exit status: 0 at or below the limit, 1 above it, and 2
        when the input cannot support a measurement.
    """
    args = _parse_args(argv)
    try:
        capture = parse_capture(args.folded)
        rules = load_rules(args.rules)
        result = summarize(capture, rules)
    except _ProfileInputError as exc:
        print(f"inconclusive: {exc}", file=sys.stderr)
        return _EXIT_INCONCLUSIVE
    document = json.dumps(result, indent=2, sort_keys=True)
    args.output.write_text(document + "\n")
    print(document)
    return _exit_status(result)


if __name__ == "__main__":
    raise SystemExit(main())
