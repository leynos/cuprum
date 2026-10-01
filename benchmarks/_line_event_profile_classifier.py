"""Classification of a parsed capture against a construction-rule set.

This is the middle of the gate. :mod:`benchmarks._line_event_profile_model`
parses a capture and a rules file into immutable shapes;
:mod:`benchmarks.summarize_line_event_profile` is the command-line front end
that writes the result document. This module turns the parsed shapes into the
weighted counts the gate is judged on, and decides the run's status.

The split exists at this seam because the front end had grown past the
project's 400-line module ceiling, and because the boundary is real: nothing
here reads a file, parses text, or knows about command-line arguments. The
whole module is a pure function of two already-parsed inputs.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from benchmarks._line_event_profile_model import FramePattern

# These appear only in annotations. Unlike the model's public dataclasses,
# nothing here is resolved through ``typing.get_type_hints`` by a consumer, so
# the imports can stay type-checking-only; this matches the sibling model
# module's handling of ``pathlib``. ``FramePattern`` is deliberately *not*
# here: the hook-dispatch boundary is a module-level tuple of live instances
# rather than an annotation, so it needs the real class at import time.
if typ.TYPE_CHECKING:
    from benchmarks._line_event_profile_model import (
        Capture,
        ClassificationRule,
        ClassifierRules,
        Frame,
        Stack,
    )

# The gate: construction must be at most this share of the consume subtree.
# The plan fixes this threshold, so it is a constant rather than an option.
#
# Revised 10.0 -> 28.0 -> 30.0, both 2026-09-27, with user approval. The
# retained per-line `ExecEvent` construction that V2/V4's observation contract
# requires puts a floor of roughly 24-27% on the achievable share, so 10% was
# unreachable by this design. The completed hoist measured 29.91% (three
# matched pairs, candidate range 0.0423 points), which cleared the floor the
# projection predicted but missed 28%; 30.0 is the approved target, sited
# above the measured result rather than on a further projection. The
# derivation, the measured miss, and the options for closing it are in
# docs/execplans/5-2-1-hoist-the-invariant-exec-event-and-event-details.md.
CONSTRUCTION_SHARE_LIMIT_PERCENT = 30.0

# The observe-hook dispatch boundary: a frame whose *matching emission caller*
# sits above one of these on the same stack is reached by way of hook dispatch.
#
# A rule's caller requirement alone cannot exclude this case. An observe hook
# runs below the dispatcher and so below every emission caller, which means a
# hook body that builds its own per-event dataclass still has that emission
# caller as its *nearest* match. Such a construction is hook-owned work, not
# event emission, and must not enter the numerator.
#
# Matching both fields matters. Neither boundary function recurs elsewhere in
# the tree today, so the location is defensive rather than load-bearing: it
# keeps a future same-named frame -- a plugin, a vendored copy, a second
# dispatcher -- from tripping the guard. Same-name recurrence is a live hazard
# here rather than a hypothetical, since `emit` is already defined in both
# `cuprum/_pipeline_types.py` and `cuprum/_line_stream/telemetry.py`.
#
# These are the two frames the observe path passes through, taken from the
# definitions rather than from a capture so an inlined or re-laid-out
# dispatcher cannot quietly drop the guard:
# `_emit_event` at `cuprum/_pipeline_types.py:220` and `_emit_exec_event`, a
# module-level function, at `cuprum/_observability.py:88`. Cite the frame, not
# the number: merging main moved both bodies, and only the name-and-location
# match above survived that.
_HOOK_DISPATCH_BOUNDARY: tuple[FramePattern, ...] = (
    FramePattern(function="_emit_event", location="cuprum/_pipeline_types.py"),
    FramePattern(function="_emit_exec_event", location="cuprum/_observability.py"),
)


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


def _crosses_hook_dispatch(
    frames: tuple[Frame, ...],
    index: int,
    caller_index: int,
) -> bool:
    """Return whether a hook-dispatch frame intervenes on the caller path.

    The path runs from a matched caller at ``caller_index`` down to the
    candidate frame at ``index``. A boundary frame anywhere strictly between
    them means the candidate is reached by way of hook dispatch rather than by
    direct emission, so it is hook-owned construction and not event emission.

    The boundary is the two-frame dispatch seam, not one frame. ``_emit_event``
    normally hands off to ``_emit_exec_event``, which is what invokes the
    hooks, but ``_emit_exec_event`` can also be reached without it, so either
    frame alone marks the boundary.

    Returns
    -------
    bool
        True when the candidate must not count toward construction.
    """
    return any(
        boundary.matches(frames[inner_index])
        for inner_index in range(caller_index + 1, index)
        for boundary in _HOOK_DISPATCH_BOUNDARY
    )


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

    A near match is rejected outright when the observe-hook dispatcher sits
    between the caller and the frame. That keeps the *nearest matching caller*
    criterion honest: the caller the search latches onto must be one that
    actually reaches the constructor, not one stranded above a boundary.

    Returns
    -------
    ClassificationRule | None
        The winning rule, or ``None`` when no rule both matches the frame and
        has a caller earlier on the stack that reaches it without crossing the
        observe-hook dispatch boundary.
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
        if _crosses_hook_dispatch(frames, index, depth):
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
    :func:`classify_capture` counts a stack at most once, so a stack counts
    toward ``construction_samples`` once however many of its frames resolve.

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
