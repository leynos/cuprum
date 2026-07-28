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
    "timeout",
    "teardown_error",
]

# A stable, per-execution correlation token. It is minted once when an
# execution begins and propagated unchanged through every lifecycle event
# (``start``, ``stdout``, ``stderr``, ``exit``) for that execution, so
# consumers can correlate the events of a single execution even when the
# operating system recycles a process identifier across executions.
ExecId = typ.NewType("ExecId", uuid.UUID)


def new_exec_id() -> ExecId:
    """Return a fresh, process-unique execution correlation token.

    Returns
    -------
    ExecId
        A new :data:`ExecId` wrapping a random UUID (:func:`uuid.uuid4`),
        distinct on every call, used to correlate all lifecycle events of a
        single execution.
    """
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

        The ``timeout`` phase marks a run that exceeded its deadline and the
        ``teardown_error`` phase marks a stream consumer that drained with an
        unexpected error during cleanup; both are ancillary diagnostics emitted
        before the existing ``exit`` event and the public ``TimeoutExpired``,
        which are preserved.
    tags:
        Arbitrary, JSON-like metadata associated with this execution.
    note:
        Optional human-readable diagnostic string for ancillary events
        such as ``stdin_error``.
    byte_count:
        Number of bytes written for byte-counted phases such as ``stdin``.
    operation:
        For failure and lifecycle events, the operation involved. For
        ``stdin_error`` this is the pipe operation that failed (for example
        ``write`` or ``close``); for ``timeout`` it is ``wait``; for
        ``teardown_error`` it is ``drain``.
    error_type:
        For failure events such as ``stdin_error``, ``timeout``, and
        ``teardown_error``, the class name of the raised exception (for example
        ``OSError``, ``TimeoutError``). For ``teardown_error`` this is the
        comma-joined class names of the consumer drain failures.
    timeout_s:
        For the ``timeout`` phase, the configured wall-clock timeout in seconds
        that was exceeded. ``None`` for other phases.
    timeout_mode:
        For the ``timeout`` phase, the reason the deadline expired:
        ``"elapsed_deadline"`` when a positive wall-clock deadline elapsed, or
        ``"non_positive_immediate"`` when a non-positive (``timeout <= 0``)
        deadline expired immediately without awaiting the process. ``None`` for
        other phases.
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
    operation: str | None = None
    error_type: str | None = None
    timeout_s: float | None = None
    timeout_mode: str | None = None
    exec_id: ExecId | None = None


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = ["ExecEvent", "ExecHook", "ExecId", "ExecPhase", "new_exec_id"]
