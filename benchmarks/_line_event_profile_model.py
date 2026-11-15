"""Shapes and parsing for the 5.2.1 construction-share classifier.

This is the parsing half of the gate: the parsers that build the immutable
shapes a capture and a rule set are parsed into, and the validation that
rejects input too weak to support a measurement. The shapes themselves live in
:mod:`benchmarks._line_event_profile_shapes` and are imported here, so the
parsers, their callers, and the tests that check them keep one import surface.
The classifying and reporting half lives in
:mod:`benchmarks.summarize_line_event_profile`, which re-exports these names so
callers have one entry point.

Definitions
-----------
The denominator ``D`` is the weighted sum of every stack containing the
per-line consume symbol. The numerator ``N`` is the weighted sum of those same
stacks that *also* contain an event-construction frame. Each stack counts once
toward ``N`` even when it contains several matching frames, so a stack cannot
inflate the numerator by matching more than once.

Every non-stack record is excluded from both, and the two percentages report
``N/D`` and ``N`` over all parent samples respectively.

Why identification is caller-based
----------------------------------
``@dataclasses.dataclass`` generates ``__init__`` with :func:`exec`, so its
code object's filename is the literal string ``"<string>"`` and every generated
constructor in the process renders identically in a py-spy capture. A blanket
match on ``__init__`` would therefore count unrelated constructors, and a match
on the frame text cannot tell ``ExecEvent`` from any other dataclass.

The rules file answers that by pairing each generated frame with the *caller*
frames that reach it. A frame matches a rule when its function name, source
file, and line identifier agree with the rule's frame pattern, and at least one
of the frames preceding it on the same stack agrees with one of the rule's
callers. Initialization that a refactor moves into a helper still matches, as
long as some caller in the rule's list remains on the path — which is why the
caller lists name every production site, not just the immediate one.

What the caller rule alone cannot tell apart
--------------------------------------------
An observe hook runs below the dispatcher and so below ``emit_line``, so a hook
that builds its own dataclass per event renders as a generated ``__init__`` whose
nearest matching caller is still ``emit_line`` — and it would be counted as
``ExecEvent`` construction. No frame pattern can separate the two, because hook
bodies are not identifiable from a capture. ``_crosses_hook_dispatch`` in
:mod:`benchmarks._line_event_profile_classifier` rejects that path shape instead,
bounding the claim without proving it; the committed measurement is unchanged,
and ``docs/profiling/5-2-1-line-event-emission`` records the re-run.
"""

from __future__ import annotations

import json
import typing as typ

from benchmarks._line_event_profile_shapes import (
    Capture,
    CaptureReading,
    ClassificationRule,
    ClassifierRules,
    Frame,
    FramePattern,
    Stack,
    _ProfileInputError,
)
from benchmarks.summarize_folded import _parse_folded_line

if typ.TYPE_CHECKING:
    import pathlib as pth


# A rule must name at least one caller. Without one, a generated frame would
# match on its own indistinguishable ``<string>`` identity.
_MIN_RULE_CALLERS = 1


def parse_frame(text: str) -> Frame:
    """Parse one rendered frame into its parts.

    py-spy renders ``function (location:line)``, where the parenthesized part
    may be absent. A trailing parenthesized group is treated as a location and
    line only when it contains a colon whose right-hand side is non-empty;
    otherwise the whole text is the function name, which keeps a function
    genuinely named with parentheses from being mangled.

    Returns
    -------
    Frame
        The frame's parts; location and line are empty when the profiler
        rendered no source location.
    """
    stripped = text.strip()
    if not stripped.endswith(")"):
        return Frame(function=stripped, location="", line="", text=stripped)
    opener = stripped.rfind(" (")
    if opener == -1:
        return Frame(function=stripped, location="", line="", text=stripped)
    location, _, line = stripped[opener + 2 : -1].rpartition(":")
    if not location:
        return Frame(function=stripped, location="", line="", text=stripped)
    return Frame(
        function=stripped[:opener],
        location=location,
        line=line,
        text=stripped,
    )


def _parse_stack_line(line: str) -> Stack | None:
    """Parse one folded-stack line, or return ``None`` when it is not one.

    The line splitting is delegated to the generic
    :func:`benchmarks.summarize_folded._parse_folded_line` so both summarizers
    agree on what a counted stack is; only the per-frame structure is added
    here, since the generic parser deliberately treats frames as opaque text.

    Returns
    -------
    Stack | None
        The parsed stack, or ``None`` when the line is not a counted stack.
    """
    parsed = _parse_folded_line(line)
    if parsed is None:
        return None
    frame_texts, samples = parsed
    return Stack(
        frames=tuple(parse_frame(text) for text in frame_texts),
        samples=samples,
    )


def _parse_frameless_weight(line: str) -> int | None:
    """Return the weight of a frame-less py-spy record, or ``None``.

    py-spy emits a record whose stack is empty — a bare weight such as
    ``" 34"`` — when a sample carries no resolvable frames. It is a
    well-formed sample the summarizers deliberately drop rather than a line
    that failed to parse, so it must not be reported as unreadable.

    Returns
    -------
    int | None
        The record's sample weight, or ``None`` when the line is not a
        frame-less record.
    """
    stripped = line.strip()
    if not stripped:
        return None
    _, separator, _ = stripped.rpartition(" ")
    if separator:
        return None
    try:
        weight = int(stripped)
    except ValueError:
        return None
    return weight if weight > 0 else None


def read_capture(folded_path: pth.Path) -> CaptureReading:
    """Read a folded-stack capture, keeping the lines nothing could parse.

    Returns
    -------
    CaptureReading
        Every parsable stack with the sum of their sample weights, and the
        non-blank lines that were neither a counted stack nor py-spy's
        frame-less record.

    Raises
    ------
    _ProfileInputError
        If the file cannot be read or holds no parsable stack.
    """
    try:
        text = folded_path.read_text(errors="replace")
    except OSError as exc:
        msg = f"cannot read folded stacks at {folded_path}: {exc}"
        raise _ProfileInputError(msg) from exc

    stacks: list[Stack] = []
    unparsed: list[str] = []
    for line in text.splitlines():
        parsed = _parse_stack_line(line)
        if parsed is not None:
            stacks.append(parsed)
        elif line.strip() and _parse_frameless_weight(line) is None:
            unparsed.append(line.strip())
    if not stacks:
        msg = f"no parsable stack records in {folded_path}"
        raise _ProfileInputError(msg)
    capture = Capture(
        stacks=tuple(stacks),
        parent_samples=sum(stack.samples for stack in stacks),
    )
    return CaptureReading(capture=capture, unparsed=tuple(unparsed))


def parse_capture(folded_path: pth.Path) -> Capture:
    """Parse a folded-stack capture into its stacks and parent-sample total.

    Returns
    -------
    Capture
        Every parsable stack, with the sum of their sample weights.

    Notes
    -----
    A capture that cannot be read, or that holds no parsable stack, raises
    ``_ProfileInputError`` from :func:`read_capture`. The discarded lines are
    not reported here; a caller that must account for them reads the capture
    with that function instead.
    """
    return read_capture(folded_path).capture


def _pattern_from_json(raw: object, *, where: str) -> FramePattern:
    """Build one frame pattern from its decoded JSON object."""
    if not isinstance(raw, dict):
        msg = f"{where}: frame pattern must be an object, got {type(raw).__name__}"
        raise _ProfileInputError(msg)
    fields: dict[str, str | None] = {}
    for key in ("function", "location", "line"):
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            msg = f"{where}: frame pattern {key!r} must be a string or null"
            raise _ProfileInputError(msg)
        fields[key] = value
    return FramePattern(
        function=fields["function"],
        location=fields["location"],
        line=fields["line"],
    )


def _require_array(raw: object, *, where: str) -> list[object]:
    """Return ``raw`` as a non-empty list, or reject it with a located message."""
    if not isinstance(raw, list) or not raw:
        msg = f"{where}: expected a non-empty array"
        raise _ProfileInputError(msg)
    return typ.cast("list[object]", raw)


def _patterns_from_json(raw: object, *, where: str) -> tuple[FramePattern, ...]:
    """Build a non-empty list of frame patterns from a decoded JSON array."""
    return tuple(
        _pattern_from_json(entry, where=f"{where}[{index}]")
        for index, entry in enumerate(_require_array(raw, where=where))
    )


def _require_object(raw: object, *, where: str) -> dict[str, object]:
    """Return ``raw`` as a mapping, or reject it with a located message."""
    if not isinstance(raw, dict):
        msg = f"{where}: must be an object, got {type(raw).__name__}"
        raise _ProfileInputError(msg)
    return typ.cast("dict[str, object]", raw)


def _require_key(raw: dict[str, object], key: str, *, where: str) -> object:
    """Return ``raw[key]``, or reject a mapping that omits ``key``."""
    if key not in raw:
        msg = f"{where}: is missing {key!r}"
        raise _ProfileInputError(msg)
    return raw[key]


def _rule_name(raw: dict[str, object], *, where: str) -> str:
    """Return the rule's non-empty name, or reject the rule without one."""
    name = raw.get("name")
    if not isinstance(name, str) or not name:
        msg = f"{where}: rule needs a non-empty 'name'"
        raise _ProfileInputError(msg)
    return name


def _rule_from_json(raw: object, *, index: int) -> ClassificationRule:
    """Build one classification rule from its decoded JSON object."""
    where = f"construction_rules[{index}]"
    rule = _require_object(raw, where=where)
    name = _rule_name(rule, where=where)
    frame = _require_key(rule, "frame", where=f"{where}: rule {name!r}")
    callers = _patterns_from_json(
        _require_key(rule, "callers", where=f"{where}: rule {name!r}"),
        where=f"{where}.callers",
    )
    if len(callers) < _MIN_RULE_CALLERS:
        msg = f"{where}: rule {name!r} needs at least one caller pattern"
        raise _ProfileInputError(msg)
    return ClassificationRule(
        name=name,
        frame=_pattern_from_json(frame, where=f"{where}.frame"),
        callers=callers,
    )


def _load_rules_json(rules_path: pth.Path) -> object:
    """Decode the rules file, rejecting unreadable or malformed input.

    The encoding is pinned rather than inherited from the locale, so a rules
    file cannot decode one way here and another way in Continuous Integration.
    Strict reading is deliberate: a mangled rule that silently matched fewer
    frames would understate the share.

    ``UnicodeDecodeError`` descends from ``ValueError``, not ``OSError``, so it
    needs catching here: as a traceback it would exit 1, the status this gate
    reserves for "share above the limit", rather than the documented 2. It
    shares the unreadable-file arm because both mean the rules never loaded,
    and CodeScene reads a third handler as pushing this function's cyclomatic
    complexity past its threshold.

    Returns
    -------
    object
        The decoded JSON document, not yet validated into rule shapes.

    Raises
    ------
    _ProfileInputError
        If the file cannot be read, cannot be decoded, or is not valid JSON.
    """
    try:
        return json.loads(rules_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"cannot read rules at {rules_path}: {exc}"
        raise _ProfileInputError(msg) from exc
    except json.JSONDecodeError as exc:
        msg = f"rules at {rules_path} are not valid JSON: {exc}"
        raise _ProfileInputError(msg) from exc


def _rules_from_json(raw: object, *, where: str) -> tuple[ClassificationRule, ...]:
    """Build the ordered construction rules from a decoded JSON array."""
    return tuple(
        _rule_from_json(entry, index=index)
        for index, entry in enumerate(_require_array(raw, where=where))
    )


def load_rules(rules_path: pth.Path) -> ClassifierRules:
    """Load and validate a classifier rule set.

    Returns
    -------
    ClassifierRules
        The validated consume-subtree patterns and construction rules.

    Raises
    ------
    _ProfileInputError
        If the file cannot be read, is not valid JSON, or omits either
        ``consume_frames`` or ``construction_rules``.
    """
    raw = _load_rules_json(rules_path)
    if not isinstance(raw, dict):
        msg = f"rules at {rules_path} must be a JSON object"
        raise _ProfileInputError(msg)
    document = typ.cast("dict[str, object]", raw)
    return ClassifierRules(
        consume_frames=_patterns_from_json(
            _require_key(document, "consume_frames", where=f"rules at {rules_path}"),
            where="consume_frames",
        ),
        construction_rules=_rules_from_json(
            _require_key(
                document,
                "construction_rules",
                where=f"rules at {rules_path}",
            ),
            where="construction_rules",
        ),
    )
