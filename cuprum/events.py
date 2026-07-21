"""Structured execution events for observability integrations.

Cuprum surfaces an optional stream of structured events describing command and
pipeline execution. These are intended for logging, metrics, tracing, and
auditing integrations without coupling Cuprum to a specific telemetry stack.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ
import uuid

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.program import Program

type ExecPhase = typ.Literal[
    "plan",
    "start",
    "stdout",
    "stderr",
    "exit",
    "stdin",
    "stdin_error",
]

# A stable, per-execution correlation token. It is minted once when an
# execution begins and propagated unchanged through every lifecycle event
# (``start``, ``stdout``, ``stderr``, ``exit``) for that execution, so
# consumers can correlate the events of a single execution even when the
# operating system recycles a process identifier across executions.
ExecId = typ.NewType("ExecId", uuid.UUID)


def new_exec_id() -> ExecId:
    """Return a fresh, process-unique execution correlation token."""
    return ExecId(uuid.uuid4())


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
    note:
        Optional human-readable diagnostic string for ancillary events
        such as ``stdin_error``.
    byte_count:
        Number of bytes written for byte-counted phases such as ``stdin``.
    exec_id:
        Stable per-execution correlation token, minted once per execution and
        shared by every lifecycle event for that execution. It is the reliable
        way to correlate an execution's events, because a process identifier
        (``pid``) can be recycled by the operating system across executions.
        ``None`` for legacy or manually constructed events that predate the
        token; such events cannot be safely correlated by consumers.

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
    note: str | None = None
    byte_count: int | None = None
    exec_id: ExecId | None = None


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = ["ExecEvent", "ExecHook", "ExecId", "ExecPhase", "new_exec_id"]
