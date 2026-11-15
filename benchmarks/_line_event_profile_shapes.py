"""The immutable shapes a 5.2.1 capture and rule set are parsed into.

The parsers that build these shapes, and the validation that rejects input too
weak to support a measurement, live in
:mod:`benchmarks._line_event_profile_model`, which imports them from here. The
split keeps each module small enough to read whole: the shapes are data, and
the parsers are the policy that decides what the data may contain.

A shape is frozen and slotted, so a parser cannot mutate one after it is
validated, and a capture shared between the census and the classifier cannot be
edited by either.
"""

from __future__ import annotations

import dataclasses as dc


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


@dc.dataclass(frozen=True, slots=True)
class CaptureReading:
    """A parsed capture together with what the parser could not read.

    Reading a capture is lossy: a line the folded-stack parser rejects is
    silently absent from :attr:`capture`, so a caller cannot tell a capture it
    understood from one it partly skipped. A measurement whose whole point is
    that a weight is *zero* must not make that mistake, so this type keeps the
    rejected lines and lets the caller decide whether they matter.

    Attributes
    ----------
    capture:
        The stacks that parsed, with the sum of their sample weights.
    unparsed:
        Every non-blank line that was neither a counted stack nor the
        frame-less record py-spy emits for a sample with no resolvable frames,
        stripped, in file order.

    """

    capture: Capture
    unparsed: tuple[str, ...]


__all__ = [
    "Capture",
    "CaptureReading",
    "ClassificationRule",
    "ClassifierRules",
    "Frame",
    "FramePattern",
    "Stack",
]
