"""Count capture samples under an anchor frame and on a target frame.

Roadmap item 5.2.2 asks whether removing the per-hook ``inspect.isawaitable``
test from the per-line dispatch removes every sample that reaches that detector
on the hot path. This script answers it from one py-spy raw-stack capture and
reports four weights, each counted once per stack:

``anchor_weight``
    Samples whose stack contains the anchor frame.
``target_weight``
    Samples whose stack contains the anchor *and* a target frame rendered with
    the target's own location.
``target_weight_any_location``
    The same, matching the target by function name alone. py-spy renders
    ``inspect.py`` line numbers that differ between interpreters, so a capture
    from another interpreter must still yield a comparable figure.
``target_weight_outside_anchor``
    Samples that reach the target without passing through the anchor, which is
    what shows the detector is *only* reachable from the hot path.

The two target weights must agree. A disagreement means the location-specific
pattern missed frames the name-only pattern found, so the census cannot say
what it measured and reports an inconclusive result instead of a number.

The anchor must also carry enough weight to support the claim: observing zero
target samples is only evidence of absence when the anchored region was
sampled often enough that a surviving target would very likely have been seen.
The caller supplies that floor, derived from the control's measured target
share, rather than inheriting a default that could silently weaken the claim.

Exit status
-----------
0
    Both target weights are zero and the anchor meets the floor.
1
    The target weights agree, are non-zero, and the anchor meets the floor.
2
    Malformed input, an anchor below the floor, or disagreeing target weights:
    the run is inconclusive and must not be read as a passing measurement.
"""

from __future__ import annotations

import argparse
import dataclasses as dc
import json
import pathlib as pth
import sys
import typing as typ

from benchmarks._line_event_profile_model import (
    Capture,
    FramePattern,
    Stack,
    _ProfileInputError,
    parse_capture,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_EXIT_PASS = 0
_EXIT_TARGET_PRESENT = 1
_EXIT_INCONCLUSIVE = 2

_SPEC_SEPARATOR = "@"

_STATUS_TARGET_ABSENT = "pass"
_STATUS_TARGET_PRESENT = "fail_target_present"
_STATUS_ANCHOR_BELOW_FLOOR = "anchor_below_floor"
_STATUS_WEIGHTS_DISAGREE = "target_weights_disagree"


@dc.dataclass(frozen=True, slots=True)
class Census:
    """The four weights one capture reports, plus the sampled total.

    Attributes
    ----------
    total_weight:
        Every parent sample the capture holds.
    anchor_weight:
        Samples whose stack contains the anchor frame.
    target_weight:
        Samples whose stack contains the anchor and a target frame at the
        target's own location.
    target_weight_any_location:
        Samples whose stack contains the anchor and the target function at any
        location.
    target_weight_outside_anchor:
        Samples that reach the target without passing through the anchor.

    """

    total_weight: int
    anchor_weight: int
    target_weight: int
    target_weight_any_location: int
    target_weight_outside_anchor: int

    def as_document(self, status: str) -> dict[str, object]:
        """Return the JSON-ready result document under ``status``."""
        return {
            "anchor_weight": self.anchor_weight,
            "status": status,
            "target_weight": self.target_weight,
            "target_weight_any_location": self.target_weight_any_location,
            "target_weight_outside_anchor": self.target_weight_outside_anchor,
            "total_weight": self.total_weight,
        }

    @property
    def status(self) -> str:
        """The verdict the weights support, without the floor test."""
        if self.target_weight != self.target_weight_any_location:
            return _STATUS_WEIGHTS_DISAGREE
        if self.target_weight == 0:
            return _STATUS_TARGET_ABSENT
        return _STATUS_TARGET_PRESENT


def _split_spec(spec: str) -> tuple[str, str] | None:
    """Split ``FUNCTION@LOCATION`` into its halves, or ``None`` if malformed."""
    function, separator, location = spec.partition(_SPEC_SEPARATOR)
    if not separator or _SPEC_SEPARATOR in location:
        return None
    if not function or not location:
        return None
    return function, location


def parse_spec(spec: str, *, option: str) -> FramePattern:
    """Parse ``FUNCTION@LOCATION`` into an exact frame pattern.

    Returns
    -------
    FramePattern
        A pattern matching both the function name and the location.

    Raises
    ------
    _ProfileInputError
        If ``spec`` has no separator, has more than one, or leaves either side
        empty. A silently truncated spec would match the wrong frames.
    """
    halves = _split_spec(spec)
    if halves is None:
        msg = f"{option}: expected FUNCTION@LOCATION, got {spec!r}"
        raise _ProfileInputError(msg)
    function, location = halves
    return FramePattern(function=function, location=location)


def _has(stack: Stack, pattern: FramePattern) -> bool:
    """Return whether the stack's frames include one ``pattern`` matches."""
    return any(pattern.matches(frame) for frame in stack.frames)


def _weigh(
    capture: Capture,
    pattern: FramePattern,
    *,
    anchor: FramePattern | None = None,
    require_anchor: bool = False,
) -> int:
    """Sum the samples of stacks ``pattern`` matches, once per stack."""
    total = 0
    for stack in capture.stacks:
        if not _has(stack, pattern):
            continue
        if anchor is not None and _has(stack, anchor) != require_anchor:
            continue
        total += stack.samples
    return total


def census(capture: Capture, anchor: FramePattern, target: FramePattern) -> Census:
    """Count the anchor and target weights in ``capture``.

    Returns
    -------
    Census
        The four weights and the capture's total sample weight.
    """
    name_only = FramePattern(function=target.function)
    return Census(
        total_weight=capture.parent_samples,
        anchor_weight=_weigh(capture, anchor),
        target_weight=_weigh(capture, target, anchor=anchor, require_anchor=True),
        target_weight_any_location=_weigh(
            capture, name_only, anchor=anchor, require_anchor=True
        ),
        target_weight_outside_anchor=_weigh(
            capture, target, anchor=anchor, require_anchor=False
        ),
    )


def _floor_status(census_result: Census, min_anchor_weight: int) -> str:
    """Return the status once the anchor floor is taken into account."""
    if census_result.anchor_weight < min_anchor_weight:
        return _STATUS_ANCHOR_BELOW_FLOOR
    return census_result.status


def _exit_status(status: str) -> int:
    """Map a status onto the process exit status."""
    if status == _STATUS_TARGET_ABSENT:
        return _EXIT_PASS
    if status == _STATUS_TARGET_PRESENT:
        return _EXIT_TARGET_PRESENT
    return _EXIT_INCONCLUSIVE


def _parse_args(argv: cabc.Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folded", type=pth.Path, help="py-spy raw stack capture")
    parser.add_argument(
        "--anchor",
        required=True,
        help="anchor frame as FUNCTION@LOCATION",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="target frame as FUNCTION@LOCATION",
    )
    parser.add_argument(
        "--min-anchor-weight",
        type=int,
        required=True,
        help="samples the anchor must carry for zero target weight to be evidence",
    )
    parser.add_argument(
        "--output", type=pth.Path, required=True, help="result JSON destination"
    )
    return parser.parse_args(argv)


def _load_inputs(
    args: argparse.Namespace,
) -> tuple[Capture, FramePattern, FramePattern]:
    """Parse the floor, capture, anchor, and target into usable values."""
    if args.min_anchor_weight < 0:
        msg = f"--min-anchor-weight must not be negative: {args.min_anchor_weight}"
        raise _ProfileInputError(msg)
    return (
        parse_capture(args.folded),
        parse_spec(args.anchor, option="--anchor"),
        parse_spec(args.target, option="--target"),
    )


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Census one capture and write the result document.

    Returns
    -------
    int
        The process exit status: 0 for a proven absence, 1 for a proven
        presence, and 2 when the input cannot support either verdict.
    """
    args = _parse_args(argv)
    try:
        capture, anchor, target = _load_inputs(args)
    except _ProfileInputError as exc:
        print(f"inconclusive: {exc}", file=sys.stderr)
        return _EXIT_INCONCLUSIVE
    result = census(capture, anchor, target)
    status = _floor_status(result, args.min_anchor_weight)
    rendered = json.dumps(result.as_document(status), indent=2, sort_keys=True)
    args.output.write_text(rendered + "\n")
    print(rendered)
    return _exit_status(status)


if __name__ == "__main__":
    raise SystemExit(main())
