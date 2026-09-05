"""Line events and the hook contract for line-level command output.

``LineEvent`` is the frozen payload delivered for every decoded output line,
whether through a :class:`~cuprum.sh.RunOutputOptions` ``on_line`` hook or by
iterating ``SafeCmd.lines()``. It follows the ADR-008 precedent of a distinct,
separately versioned payload type rather than extending the closed
``ExecPhase`` literal: consumers matching exhaustively on ``ExecPhase`` must
not be broken by this addition, and a line event needs none of the fields a
lifecycle ``ExecEvent`` carries.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ
from time import perf_counter

type LineStreamName = typ.Literal["stdout", "stderr"]
type LineHook = cabc.Callable[[LineEvent], None]


# Bound as a module-level name rather than reached through the ``time`` module
# so tests can pin the clock by replacing this attribute alone; reaching
# through ``time.perf_counter`` would change the clock every other module in
# the process reads. Mirrors the seam pattern in ``cuprum._pipeline_wait``.
perf_counter = perf_counter


@dc.dataclass(frozen=True, slots=True)
class LineEvent:
    """One decoded output line, tagged with its stream and arrival time.

    Attributes
    ----------
    stream:
        Which stream the line arrived on: ``"stdout"`` or ``"stderr"``.
    at:
        Monotonic seconds since the command started, read from the
        :func:`time.perf_counter` clock at the moment the line was decoded.
        Values are only comparable within one command's run.
    text:
        Decoded line content without its line terminator.

    """

    stream: LineStreamName
    at: float
    text: str


__all__ = [
    "LineEvent",
    "LineHook",
    "LineStreamName",
]
