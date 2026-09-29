"""Shapes and parsing for the 5.2.1 construction-share classifier.

This is the input half of the gate: the immutable shapes a capture and a rule
set are parsed into, the parsers that build them, and the validation that
rejects input too weak to support a measurement. The classifying and reporting
half lives in :mod:`benchmarks.summarize_line_event_profile`, which re-exports
these names so callers have one entry point.

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

What the caller rule cannot tell apart
--------------------------------------
The caller requirement rules out an unrelated dataclass constructed anywhere else
in the process, but it does not exclude every constructor reached *through* the
event path. An observe hook runs below ``emit_line`` on the stack, so a hook
that builds its own dataclass per event renders as a generated ``__init__``
whose nearest matching caller is still ``emit_line`` — and it would be counted
toward ``N`` as though it were ``ExecEvent``. Hook bodies are not identifiable
from a capture, so no rule can separate the two cases in general.

This is a known limit of the metric, not a defect in the committed measurement.
The benchmark's own hook only increments a counter and constructs nothing, so
no such sample exists in the gate's captures: in all three candidate captures
every counted construction frame is a *direct* call from ``emit_line``, with no
intervening frame, accounting for the whole of ``N`` (15,769 weighted samples
across ``r1``-``r3``). A capture whose hook does allocate per event would
inflate ``N``, and should be read with that in mind.
"""

from __future__ import annotations

import dataclasses as dc
import json
import typing as typ

from benchmarks.summarize_folded import _parse_folded_line

if typ.TYPE_CHECKING:
    import pathlib as pth


# A rule must name at least one caller. Without one, a generated frame would
# match on its own indistinguishable ``<string>`` identity.
_MIN_RULE_CALLERS = 1


class _ProfileInputError(Exception):
    """Raised when a capture or rules file cannot support a measurement."""


@dc.dataclass(frozen=True, slots=True)
class Frame:
    """One parsed stack frame.

    Attributes
    ----------
    function:
        Function name as the profiler rendered it.
    location:
        Source file (or profile marker) the frame belongs to.
    line:
        Line identifier within ``location``, as a string, because generated
        frames carry a synthetic value rather than a real line number.
    text:
        The frame exactly as it appeared, retained for reporting.

    """

    function: str
    location: str
    line: str
    text: str


@dc.dataclass(frozen=True, slots=True)
class FramePattern:
    """A frame matcher: any field left as ``None`` matches anything."""

    function: str | None = None
    location: str | None = None
    line: str | None = None

    def matches(self, frame: Frame) -> bool:
        """Return whether ``frame`` satisfies every field this pattern sets."""
        return all(
            (
                self.function is None or self.function == frame.function,
                self.location is None or self.location == frame.location,
                self.line is None or self.line == frame.line,
            ),
        )


@dc.dataclass(frozen=True, slots=True)
class ClassificationRule:
    """One construction site: a frame pattern plus the callers reaching it.

    Attributes
    ----------
    name:
        Human-readable label reported with the matches.
    frame:
        The frame considered to be construction work.
    callers:
        Frame patterns for callers, at least one of which must appear earlier
        on the same stack for ``frame`` to count.

    """

    name: str
    frame: FramePattern
    callers: tuple[FramePattern, ...]


@dc.dataclass(frozen=True, slots=True)
class ClassifierRules:
    """The full rule set: what to count, and which stacks form the subtree."""

    consume_frames: tuple[FramePattern, ...]
    construction_rules: tuple[ClassificationRule, ...]


@dc.dataclass(frozen=True, slots=True)
class Stack:
    """A parsed capture line: one stack of frames and its sample weight."""

    frames: tuple[Frame, ...]
    samples: int


@dc.dataclass(frozen=True, slots=True)
class Capture:
    """A parsed capture: its stacks and its total parent sample weight."""

    stacks: tuple[Stack, ...]
    parent_samples: int


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


def parse_capture(folded_path: pth.Path) -> Capture:
    """Parse a folded-stack capture into its stacks and parent-sample total.

    Returns
    -------
    Capture
        Every parsable stack, with the sum of their sample weights.

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
    for line in text.splitlines():
        parsed = _parse_stack_line(line)
        if parsed is not None:
            stacks.append(parsed)
    if not stacks:
        msg = f"no parsable stack records in {folded_path}"
        raise _ProfileInputError(msg)
    return Capture(stacks=tuple(stacks), parent_samples=sum(s.samples for s in stacks))


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
    """Decode the rules file, rejecting unreadable or malformed input."""
    try:
        return json.loads(rules_path.read_text())
    except OSError as exc:
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
