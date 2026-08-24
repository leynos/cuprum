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

# ``plan`` … ``stdin_error`` describe one command's own lifecycle.
# ``pipeline_fail_fast`` is different in kind: it reports a decision the
# pipeline coordinator took about a stage: its non-zero exit was the first
# failure and every other still-running stage — upstream producer and downstream
# consumer alike — is about to be torn down. The stage's own ``exit`` event
# still follows.
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
    "pipeline_fail_fast",
]

# A stable, per-execution correlation token. It is minted once when an
# execution begins and propagated unchanged through every lifecycle event
# (``start``, ``stdout``, ``stderr``, ``exit``) for that execution, so
# consumers can correlate the events of a single execution even when the
# operating system recycles a process identifier across executions.
ExecId = typ.NewType("ExecId", uuid.UUID)

# The stable ``timeout_mode`` values, shared by the ``timeout`` observe event
# and the ``cuprum.timeout`` log records so a consumer keying on either sees
# the same two strings. ``elapsed_deadline`` is a positive wall-clock deadline
# that ran out; ``non_positive_immediate`` is a ``timeout <= 0`` that expired
# at once, without ever awaiting the process.
type TimeoutMode = typ.Literal["elapsed_deadline", "non_positive_immediate"]


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
        Event phase. See :data:`~cuprum.events.ExecPhase`. Both ``timeout`` and
        ``teardown_error`` are ancillary diagnostics that never displace a
        lifecycle phase. ``pipeline_fail_fast`` reports the pipeline
        coordinator's decision before the failing stage's own ``exit`` event.

        ``timeout`` marks a run that exceeded its deadline, and is emitted
        before the existing ``exit`` event and the public ``TimeoutExpired``,
        both of which are preserved.

        ``teardown_error`` marks a stream consumer that drained with an
        unexpected error during cleanup, and carries no such ordering
        guarantee. Cleanup also runs on external cancellation and on an
        unexpected stdin-writer failure, and on those paths the original
        exception propagates unchanged: no ``exit`` event follows and no
        ``TimeoutExpired`` is raised, so a ``teardown_error`` may be the last
        event a consumer sees for that execution.
    program:
        The allowlisted program that is executing.
    argv:
        Full argv including program name as the first element. Empty for the
        sanitized ``pipeline_fail_fast`` decision event.
    cwd:
        Working directory for the subprocess, when set. Absent from the
        sanitized ``pipeline_fail_fast`` decision event.
    env:
        Environment overlay provided for this execution, when set. Absent from
        the sanitized ``pipeline_fail_fast`` decision event.
    pid:
        Process identifier for the running subprocess (populated for every
        phase except ``plan``, which fires before the subprocess is
        spawned).
    timestamp:
        Wall-clock timestamp (seconds since epoch) when the phase occurred.
    line:
        Output line for ``stdout`` / ``stderr`` phases. Line terminators are
        omitted.
    exit_code:
        Exit code for the ``exit`` phase, and the failing stage's exit code for
        the ``pipeline_fail_fast`` phase.
    duration_s:
        Elapsed duration in seconds from ``start`` to subprocess exit (not
        including output drain after process termination). For
        ``pipeline_fail_fast`` this is how long the failing stage ran before
        its completion was observed.
    tags:
        Arbitrary, JSON-like metadata associated with this execution. Empty on
        the sanitized ``pipeline_fail_fast`` decision event.
    project:
        Trusted project name configured for the command. Unlike ``tags``, this
        value is not caller supplied and therefore remains available on the
        sanitized ``pipeline_fail_fast`` decision event.
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
    exec_id:
        Stable per-execution correlation token, minted once per execution and
        shared by every lifecycle event for that execution. It is the reliable
        way to correlate an execution's events, because a process identifier
        (``pid``) can be recycled by the operating system across executions.
        ``None`` for legacy or manually constructed events that predate the
        token; such events cannot be safely correlated by consumers.
    timeout_s:
        For the ``timeout`` phase, the configured wall-clock timeout in seconds
        that was exceeded. ``None`` for other phases.
    timeout_mode:
        For the ``timeout`` phase, the reason the deadline expired:
        ``"elapsed_deadline"`` when a positive wall-clock deadline elapsed, or
        ``"non_positive_immediate"`` when a non-positive (``timeout <= 0``)
        deadline expired immediately without awaiting the process. ``None`` for
        other phases.
    stage_index:
        Zero-based pipeline position of the failing stage for
        ``pipeline_fail_fast``. This typed field is not derived from tags, which
        callers may shadow.
    stage_count:
        Pipeline width for ``pipeline_fail_fast``.

    New optional fields are appended after ``exec_id`` rather than inserted
    beside the field they relate to. Inserting one ahead of ``exec_id`` would
    silently rebind a positional argument in existing caller code, handing the
    correlation token to the new field and leaving ``exec_id=None`` — which
    consumers such as ``TracingHook`` treat as uncorrelatable and drop.

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
    project: str | None = None
    note: str | None = None
    byte_count: int | None = None
    operation: str | None = None
    error_type: str | None = None
    exec_id: ExecId | None = None
    # Appended after exec_id to keep its positional slot stable; see the note
    # in the class docstring.
    timeout_s: float | None = None
    timeout_mode: TimeoutMode | None = None
    stage_index: int | None = None
    stage_count: int | None = None


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = [
    "ExecEvent",
    "ExecHook",
    "ExecId",
    "ExecPhase",
    "TimeoutMode",
    "new_exec_id",
]
