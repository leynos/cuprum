"""Measure the construction share of the per-line event-emission hot path.

Roadmap item 5.2.1 asks whether hoisting invariant execution metadata out of
the per-line callback brings the share of parent samples spent *constructing*
observe events under 10% of the per-line consume subtree. This script is the
gate that answers it, from one py-spy raw-stack capture.

The parsed shapes, the capture and rules parsers, and the definitions of the
numerator and denominator all live in
:mod:`benchmarks._line_event_profile_model`; this module classifies a parsed
capture and reports the result. The model's names are re-exported here so the
gate has a single entry point, and :mod:`benchmarks.summarize_folded` supplies
the shared notion of what a counted folded-stack line is.

Unresolved frames are reported rather than silently scored, and a run with no
denominator, no matched construction frames, or a malformed capture exits with
status 2 so a missing symbol can never read as a zero-cost result.

Exit status
-----------
0
    Valid capture with a construction share of at most 10%.
1
    Valid capture with a construction share above 10%.
2
    Malformed, insufficient, or unresolved input: the run is inconclusive and
    must not be read as a passing measurement.
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

# The gate: construction must be at most this share of the consume subtree.
# The plan fixes this threshold, so it is a constant rather than an option.
CONSTRUCTION_SHARE_LIMIT_PERCENT = 10.0

_EXIT_PASS = 0
_EXIT_ABOVE_LIMIT = 1
_EXIT_INCONCLUSIVE = 2

# Re-exported so the gate keeps one import surface. The model module owns the
# definitions; listing them here documents the intended entry point for
# callers and tests, which import this module rather than the model.
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


def _caller_depth(
    frames: tuple[Frame, ...],
    index: int,
    rule: ClassificationRule,
) -> int | None:
    """Return the nearest matching caller's index, or ``None`` if none match.

    The index is the highest position below ``index`` whose frame agrees with
    one of the rule's caller patterns. Requiring the caller to precede the
    frame keeps a rule from matching on a caller that runs later, which would
    mean the two frames are not in a call relationship at all.

    Returns
    -------
    int | None
        The index of the nearest matching caller, or ``None`` when no frame
        below ``index`` agrees with any of the rule's caller patterns.
    """
    for earlier_index in range(index - 1, -1, -1):
        if any(caller.matches(frames[earlier_index]) for caller in rule.callers):
            return earlier_index
    return None


def _matching_rule(
    frames: tuple[Frame, ...],
    index: int,
    rules: ClassifierRules,
) -> ClassificationRule | None:
    """Return the rule that classifies ``frames[index]``, if any.

    Several rules can match one frame: every generated constructor renders as
    ``__init__ (<string>:N)``, so two rules that differ only in their callers
    both see a candidate. The rule whose nearest matching caller is *closest*
    to the frame wins, because that caller is the one that actually invokes the
    constructor; ties break on rule order so a capture classifies
    deterministically.

    Choosing by proximity rather than by declaration order keeps the reported
    per-rule attribution independent of how the rules file happens to be
    sorted. Resolving by order alone would let a broadly-called rule silently
    absorb a narrower one's frames.

    Returns
    -------
    ClassificationRule | None
        The winning rule, or ``None`` when no rule both matches the frame and
        has a caller earlier on the stack.
    """
    frame = frames[index]
    best: ClassificationRule | None = None
    best_depth = -1
    for rule in rules.construction_rules:
        if not rule.frame.matches(frame):
            continue
        depth = _caller_depth(frames, index, rule)
        if depth is None:
            continue
        if depth > best_depth:
            best = rule
            best_depth = depth
    return best


@dc.dataclass(slots=True)
class _Accumulator:
    """Running totals for one capture classification."""

    consume_samples: int = 0
    construction_samples: int = 0
    matched_frames: dict[str, int] = dc.field(default_factory=dict)
    caller_samples: dict[str, int] = dc.field(default_factory=dict)


def _in_consume_subtree(stack: Stack, rules: ClassifierRules) -> bool:
    """Return whether any frame of ``stack`` matches a consume pattern.

    Returns
    -------
    bool
        True when the stack belongs to the denominator.
    """
    return any(
        pattern.matches(frame)
        for frame in stack.frames
        for pattern in rules.consume_frames
    )


def _record_caller_weight(
    accumulated: _Accumulator, stack: Stack, rules: ClassifierRules
) -> None:
    """Add ``stack``'s weight to every rule whose caller it contains.

    Each rule gains the stack's samples at most once, however many of its
    callers appear.
    """
    seen_callers: set[str] = set()
    for frame in stack.frames:
        for rule in rules.construction_rules:
            if rule.name in seen_callers:
                continue
            if any(caller.matches(frame) for caller in rule.callers):
                seen_callers.add(rule.name)
                accumulated.caller_samples[rule.name] = (
                    accumulated.caller_samples.get(rule.name, 0) + stack.samples
                )


def _record_matches(
    accumulated: _Accumulator, stack: Stack, rules: ClassifierRules
) -> bool:
    """Attribute ``stack`` to each matching rule that owns one of its frames.

    Each matching frame contributes the stack's full ``samples`` weight to its
    winning rule, so a stack matching ``k`` rules adds its weight ``k`` times
    across ``matched_frames`` and the per-rule counts may sum to more than the
    capture's ``construction_samples``. The numerator is not inflated with it:
    ``classify_capture`` counts a stack at most once, so a stack counts toward
    ``construction_samples`` once however many of its frames resolve.

    Returns
    -------
    bool
        True when at least one frame resolved, i.e. the stack is in the
        numerator.
    """
    counted = False
    for index in range(len(stack.frames)):
        rule = _matching_rule(stack.frames, index, rules)
        if rule is None:
            continue
        accumulated.matched_frames[rule.name] = (
            accumulated.matched_frames.get(rule.name, 0) + stack.samples
        )
        counted = True
    return counted


def classify_capture(
    capture: Capture,
    rules: ClassifierRules,
) -> _Accumulator:
    """Classify every stack, accumulating weighted totals.

    Each stack is attributed at most once to the numerator, however many
    matching frames it contains. Alongside the matches, the accumulator
    records how much weight each rule's *callers* carry inside the consume
    subtree, which is what :func:`summarize` uses to tell a rule that is
    simply absent from the workload apart from one that has drifted.

    Returns
    -------
    _Accumulator
        Weighted sample totals and per-rule counts for the whole capture.
    """
    accumulated = _Accumulator()
    for stack in capture.stacks:
        if not _in_consume_subtree(stack, rules):
            continue
        accumulated.consume_samples += stack.samples
        _record_caller_weight(accumulated, stack, rules)
        if _record_matches(accumulated, stack, rules):
            accumulated.construction_samples += stack.samples
    return accumulated


def _drifted_rules(accumulated: _Accumulator) -> dict[str, int]:
    """Return the rules whose production callers ran without any match.

    Drift is judged capture-wide and per rule, not per stack. Two weaker
    signals were tried and rejected against the real control capture:

    * Judging a *frame* unresolved when no rule claims it flags every
      generated constructor in the consume subtree, including the
      echo-truncation limiter's own records, which have nothing to do with
      event emission. Generated constructors are indistinguishable, so the
      frame alone cannot carry that judgement.
    * Judging a *stack* unresolved when a caller appears without the frame
      below it flags ordinary sampling. A sample landing anywhere inside
      ``emit_line`` before it reaches the constructor has that exact shape,
      and it is the common case, not a defect.

    What does indicate drift is a rule whose callers carry real weight in the
    consume subtree while the rule matched nothing at all: the production code
    still runs the caller, but no longer constructs what the rule names. A
    rule whose callers never appear is merely absent from this workload and is
    left alone, so a single-workload capture does not report every unrelated
    rule as broken.

    Returns
    -------
    dict[str, int]
        Each drifted rule's name mapped to its callers' weighted samples.
    """
    return {
        rule_name: accumulated.caller_samples[rule_name]
        for rule_name in accumulated.caller_samples
        if rule_name not in accumulated.matched_frames
    }


def _percent(numerator: int, denominator: int) -> float:
    """Return ``numerator`` as a percentage of ``denominator``, rounded."""
    if denominator == 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 4)


def _status(
    accumulated: _Accumulator,
    share: float,
    drifted: dict[str, int],
) -> str:
    """Return the run's status, most specific failure first.

    Drifted rules are checked before the empty-numerator case: a capture where
    the production callers ran but nothing matched reports *which* rule
    drifted, rather than the less specific "nothing matched".

    Returns
    -------
    str
        The status label reported alongside the counts.
    """
    if accumulated.consume_samples == 0:
        return "inconclusive_no_consume_samples"
    if drifted:
        return "inconclusive_drifted_rules"
    if not accumulated.matched_frames:
        return "inconclusive_no_matched_construction_frames"
    if share <= CONSTRUCTION_SHARE_LIMIT_PERCENT:
        return "pass"
    return "fail_above_limit"


def summarize(
    capture: Capture,
    rules: ClassifierRules,
) -> dict[str, object]:
    """Build the JSON result document for one capture.

    Returns
    -------
    dict[str, object]
        Weighted sample counts, both percentages, matched and unresolved
        frames, and the run's ``status``.
    """
    accumulated = classify_capture(capture, rules)
    share = _percent(accumulated.construction_samples, accumulated.consume_samples)
    all_parent_share = _percent(
        accumulated.construction_samples, capture.parent_samples
    )
    drifted = _drifted_rules(accumulated)

    return {
        "parent_samples": capture.parent_samples,
        "consume_samples": accumulated.consume_samples,
        "construction_samples": accumulated.construction_samples,
        "construction_share_percent": share,
        "construction_share_of_all_parent_percent": all_parent_share,
        "limit_percent": CONSTRUCTION_SHARE_LIMIT_PERCENT,
        "matched_frames": dict(sorted(accumulated.matched_frames.items())),
        "caller_samples": dict(sorted(accumulated.caller_samples.items())),
        "unresolved_frames": dict(sorted(drifted.items())),
        "status": _status(accumulated, share, drifted),
    }


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
