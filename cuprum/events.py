"""Structured execution events for observability integrations.

Cuprum surfaces an optional stream of structured events describing command and
pipeline execution. These are intended for logging, metrics, tracing, and
auditing integrations without coupling Cuprum to a specific telemetry stack.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ
import uuid

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.context.env_overlay import EnvMode, EnvOverlay
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
    "settled",
    "stdin",
    "stdin_error",
    "timeout",
    "teardown_error",
    "capture_eof_grace_expired",
    "pipeline_fail_fast",
]


class TerminalOutcome(enum.StrEnum):
    """Why an observed execution reached its definitive terminal event.

    The values are shared with presentation sinks, so execution telemetry and
    output adapters classify the same outcome with the same bounded labels.
    """

    EXIT_ZERO = "exit_zero"
    EXIT_NONZERO = "exit_nonzero"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    ERROR = "error"


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


# The stable ``resource_usage_mode`` values, naming how a terminal ``exit``
# event obtained the child resource figures it carries. They are shared by the
# observe event, the ``cuprum_`` log extras, the span attributes, and the
# metrics label, so a consumer keying on any of them sees the same strings.
#
# ``wait4_child`` is the attributable case: one reaped child's own usage, with
# ``max_rss_bytes`` populated. ``aggregate_cpu_delta`` is the CPU-only
# fallback, where ``RUSAGE_CHILDREN`` snapshots bracket the run and
# ``max_rss_bytes`` stays ``None`` because that high-water mark spans every
# reaped child. ``unavailable`` means the platform offers neither source.
class ResourceUsageMode(enum.StrEnum):
    """How a terminal ``exit`` event obtained the resource figures it carries.

    A closed set, like :class:`~cuprum.pump_events.RustPumpDeclineReason`: the
    value is a metric label and a log extra that operators filter on, so a
    typo at a new call site would produce a value their filters silently miss.
    As an enum it is a type error instead.

    Members are `str`, so the observe event, the ``cuprum_`` log extras, the
    span attributes, and the metrics label all keep carrying plain strings —
    the constant this replaced was a ``Literal`` of exactly these three values,
    and a member formats as its own value.

    Examples
    --------
    The member value is the string consumers see::

        assert ResourceUsageMode.WAIT4_CHILD == "wait4_child"

    """

    WAIT4_CHILD = "wait4_child"
    AGGREGATE_CPU_DELTA = "aggregate_cpu_delta"
    UNAVAILABLE = "unavailable"


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

        ``settled`` is the single definitive terminal event for every execution
        that emitted ``plan``. It carries ``terminal_outcome``, an optional
        ``pid``, and ``exec_id``. ``exit_code`` is a real child status only when
        one is available; it stays ``None`` for spawn failure and cancellation
        paths that do not produce a command result. The event never carries an
        exception payload.
        Catalogue lookup failures raised as ``UnknownProgramError`` in
        :mod:`cuprum.catalogue` and allowlist failures from
        :func:`cuprum.sh._enforce_allowlist` happen before ``exec_id`` is minted
        and are outside this terminal-event contract.

        ``timeout`` marks a run that exceeded its deadline, and is emitted
        before the existing ``exit`` event and the public ``TimeoutExpired``,
        both of which are preserved.

        ``teardown_error`` marks a stream consumer that drained with an
        unexpected error during cleanup. ``capture_eof_grace_expired`` marks a
        capturing drain whose bounded EOF grace elapsed while one or two
        readers remained pending. Neither ancillary diagnostic carries an
        ordering guarantee. Cleanup also runs on external cancellation and on
        an unexpected stdin-writer failure, and on those paths the original
        exception propagates unchanged: no ``exit`` event follows and no
        ``TimeoutExpired`` is raised, so an ancillary diagnostic may be the
        last event a consumer sees for that execution.
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
        Process identifier for the running subprocess, when reported. ``plan``
        always carries ``None``. A ``settled`` event can also carry ``None``
        when startup failed before a process existed or cancellation ended the
        run before a result was available.
    timestamp:
        Wall-clock timestamp (seconds since epoch) when the phase occurred.
    line:
        Output line for ``stdout`` / ``stderr`` phases. Line terminators are
        omitted.
    exit_code:
        Real child exit code for ``exit`` and, when available, ``settled``
        phases, and the failing stage's exit code for ``pipeline_fail_fast``.
        It is ``None`` when no child status is available, including spawn
        failure and cancellation paths that do not produce a command result.
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
    terminal_outcome:
        Closed category carried by ``settled``: ``exit_zero``,
        ``exit_nonzero``, ``timeout``, ``cancelled``, or ``error``. It is
        ``None`` for every other phase and contains no exception details.
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
    eof_grace_s:
        Fixed capture EOF grace duration for ``capture_eof_grace_expired``.
    pending_readers:
        Number of readers (one or two) still pending when the capture EOF
        grace elapsed. ``None`` for other phases.
    max_rss_bytes:
        Maximum resident set size attributable to this execution's own child,
        in bytes, as reported by ``wait4``. Carried on the terminal ``exit``
        event of a direct command on Linux and macOS. ``None`` on Windows, on
        platforms whose child-specific interface is unavailable, on pipeline
        stages whose concurrently reaped children cannot be separated, and on
        the aggregate CPU-only fallback — whose ``RUSAGE_CHILDREN`` high-water
        mark spans every reaped child and cannot be attributed to this one.
        Also ``None`` on a ``timeout`` terminal event, where the child was
        signalled rather than reaped by its owner.
    user_cpu_seconds:
        User CPU time in seconds attributable to this execution's child.
        Populated from the same ``wait4`` result as ``max_rss_bytes`` on the
        attributable path, and from the clamped ``RUSAGE_CHILDREN`` delta on
        the CPU-only fallback. ``None`` where neither source applies.
    system_cpu_seconds:
        System CPU time in seconds attributable to this execution's child,
        from the same sources and under the same conditions as
        ``user_cpu_seconds``.
    resource_usage_mode:
        How the resource fields above were obtained. See
        :data:`~cuprum.events.ResourceUsageMode`. It is carried so a consumer
        can tell an attributable measurement from the aggregate fallback and
        from a platform that measures nothing, without inferring that from a
        ``None`` in ``max_rss_bytes`` alone.

        Every terminal ``exit`` event carries a mode: ``wait4_child`` or
        ``aggregate_cpu_delta`` where a source produced figures, and
        ``unavailable`` where none did — Windows, a platform without the
        child-specific interface, a pipeline stage, and the timeout path, which
        signals its child rather than reaping it. An ``unavailable`` terminal
        event therefore still distinguishes a platform that cannot measure from
        one whose samples went missing, which a bare ``None`` cannot. The mode
        is ``None`` on every non-terminal phase.
    env_mode:
        The effective environment policy for this execution, once the active
        context and any per-call policy have been composed. Carried on every
        phase, because it is known before the child is spawned and describes
        the whole execution rather than one measurement.

        A replacement boundary is the reason the field exists: a
        ``REPLACE`` policy discards the live parent environment, so a child
        that omits ``PATH`` can fail to resolve a bare program name before it
        ever starts. Without the mode, a consumer sees an ordinary spawn
        failure and cannot tell it apart from an overlay run. ``None`` only on
        legacy or manually constructed events; the execution paths always
        resolve a mode.
    resolved_path:
        The executable this execution actually ran, when a scope bound the
        program's logical identity to a specific path. ``None`` when no
        binding applied, in which case the child ran under the name in
        ``argv[0]``.

        It is the *executable*, not the command: ``argv`` still carries the
        arguments, and ``program`` still carries the catalogue identity that
        policy was checked against. Keeping all three means a consumer can see
        what was permitted, what was asked for, and what ran, without having
        to infer one from another. The path is a string rather than a
        resolved file identity, so a replaced binary is not detected; see the
        TOCTOU note in ``cuprum.executable_binding``.

    New optional fields are appended after every field that already had a
    positional slot, which is what preserves those slots. In particular,
    inserting one ahead of ``exec_id`` would silently rebind a positional
    argument in existing caller code, handing the correlation token to the new
    field and leaving ``exec_id=None`` — which consumers such as ``TracingHook``
    treat as uncorrelatable and drop.

    Appended *after the pre-existing slots* is the whole rule, not "last in the
    class". ``terminal_outcome`` is pinned as the declaration tail by
    ``test_terminal_outcome_public_api``, so a field added after it belongs
    ahead of it instead: both are past every pre-existing slot, which is the
    invariant callers depend on.

    """

    phase: ExecPhase
    program: Program
    argv: tuple[str, ...]
    cwd: Path | None
    env: EnvOverlay | None
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
    exec_id: ExecId | None = None
    project: str | None = None
    # Appended after exec_id to keep its positional slot stable; see the note
    # in the class docstring.
    timeout_s: float | None = None
    timeout_mode: TimeoutMode | None = None
    stage_index: int | None = None
    stage_count: int | None = None
    eof_grace_s: float | None = None
    pending_readers: int | None = None
    max_rss_bytes: int | None = None
    user_cpu_seconds: float | None = None
    system_cpu_seconds: float | None = None
    resource_usage_mode: ResourceUsageMode | None = None
    env_mode: EnvMode | None = None
    # Declared before ``terminal_outcome`` rather than after it so that field
    # stays the declaration tail. ``test_terminal_outcome_public_api`` pins
    # ``dc.fields()[-1]`` to it, and a caller reading the generated signature
    # sees the same ordering. Both are appended after every pre-existing
    # positional slot, which is the invariant the class docstring states.
    resolved_path: str | None = None
    terminal_outcome: TerminalOutcome | None = None


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = [
    "ExecEvent",
    "ExecHook",
    "ExecId",
    "ExecPhase",
    "ResourceUsageMode",
    "TerminalOutcome",
    "TimeoutMode",
    "new_exec_id",
]
