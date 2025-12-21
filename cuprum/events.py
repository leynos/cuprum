"""Structured execution events for observability integrations.

Cuprum surfaces an optional stream of structured events describing command and
pipeline execution. These are intended for logging, metrics, tracing, and
auditing integrations without coupling Cuprum to a specific telemetry stack.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.program import Program

type ExecPhase = typ.Literal["plan", "start", "stdout", "stderr", "exit"]


@dc.dataclass(frozen=True, slots=True)
class ExecEvent:
    """A structured execution event emitted by Cuprum.

    Attributes
    ----------
    phase:
        Event phase. See :data:`~cuprum.events.ExecPhase`.
    program:
        The allowlisted program that is executing.
    argv:
        Full argv including program name as the first element.
    cwd:
        Working directory for the subprocess, when set.
    env:
        Environment overlay provided for this execution, when set.
    pid:
        Process identifier for the running subprocess (available for ``start``
        and ``exit`` phases).
    timestamp:
        Wall-clock timestamp (seconds since epoch) when the phase occurred.
    line:
        Output line for ``stdout`` / ``stderr`` phases. Line terminators are
        omitted.
    exit_code:
        Exit code for the ``exit`` phase.
    duration_s:
        Elapsed duration in seconds from ``start`` to subprocess exit (not
        including output drain after process termination).
    tags:
        Arbitrary, JSON-like metadata associated with this execution.

    """

    phase: ExecPhase
    program: Program
    argv: tuple[str, ...]
    cwd: Path | None
    env: cabc.Mapping[str, str] | None
    pid: int | None
    timestamp: float
    line: str | None
    exit_code: int | None
    duration_s: float | None
    tags: cabc.Mapping[str, object]


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = ["ExecEvent", "ExecHook", "ExecPhase"]
