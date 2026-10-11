"""Structured execution events for observability integrations.

Cuprum surfaces an optional stream of structured events describing command and
pipeline execution. These are intended for logging, metrics, tracing, and
auditing integrations without coupling Cuprum to a specific telemetry stack.
"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import typing as typ

# The event vocabularies are split into their own module so a consumer can
# reach a label — a phase, an outcome, a mode — without importing the payload
# they ride on. Re-exported here so every ``cuprum.events.<name>`` spelling
# this module published keeps working, and so ``ExecEvent``'s own field
# defaults and this module's ``__all__`` can still name them locally.
from cuprum._event_taxonomy import (
    CaptureMode,
    ExecId,
    ExecPhase,
    GroupTeardownOutcome,
    ResourceUsageMode,
    TerminalOutcome,
    TimeoutMode,
    new_exec_id,
)
from cuprum.stdio_events import StdioFailureCategory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.context.env_overlay import EnvMode, EnvOverlay
    from cuprum.program import Program


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

        ``stdio_error`` is the best-effort counterpart to ``stdin_error`` for
        the standard-stream boundaries that fail *outside* the stdin writer's
        own pipe operations: the caller's producer, an invalid chunk, the
        encoder, opening a cuprum-owned target file, and flushing a caller's
        borrowed file object. It is emitted where the failing boundary is
        known, and never in place of the caller-visible exception — a producer
        failure still raises ``StdinSourceError`` with the producer's own
        exception chained, and a failed owned-path open still raises the
        ``OSError`` :func:`~cuprum._stdio_plan._open_owned_path` builds. It
        carries ``error_category`` rather than an exception message, because
        several of these boundaries raise the same exception type and the
        category is what separates them.

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
    owns_group:
        Whether the teardown that produced a ``group_teardown`` event was
        signalling the child's whole process group rather than the direct child
        alone. ``None`` for other phases. Carried because the same outcome means
        different things on the two routes: a group left holding members is a
        failure the ownership policy introduced, while a direct-child signal
        that could not be delivered is the behaviour every run had before it.
    group_outcome:
        Closed category for a ``group_teardown`` event. See
        :class:`~cuprum.events.GroupTeardownOutcome`. It is this field, rather
        than ``note`` or ``error_type``, that a bounded metric is labelled
        from, so no pid, path, or error string can reach a metric label.
        ``None`` for other phases.
    settle_s:
        How long the group-settlement wait had run when it expired, carried by
        a ``settlement_timeout`` event. ``None`` on the other outcomes, so an
        absent figure is not read as an instant one.
    resolved_path:
        The executable this execution actually ran, when a scope bound the
        program's logical identity to a specific path. ``None`` when no
        binding applied, in which case the child ran under the name in
        ``argv[0]``.
    error_category:
        Bounded category naming which failure boundary produced a diagnostic
        event. See :class:`~cuprum.stdio_events.StdioFailureCategory`, also
        re-exported here. Carried on ``stdio_error``; ``None`` for every other
        phase, including the pre-existing ``stdin_error`` events, whose
        ``operation`` already names its boundary. It is a typed field rather
        than part of ``note`` because ``error_type`` cannot carry it, and the
        value is low-cardinality, so it is the one failure attribute safe to
        attach to a metric series.

        It is the *executable*, not the command: ``argv`` still carries the
        arguments, and ``program`` still carries the catalogue identity that
        policy was checked against. Keeping all three means a consumer can see
        what was permitted, what was asked for, and what ran, without having
        to infer one from another. The path is a string rather than a
        resolved file identity, so a replaced binary is not detected; see the
        TOCTOU note in ``cuprum.executable_binding``.
    capture_mode:
        The mode this run reports its captures in, carried on every phase for
        the reason ``env_mode`` is: it is decided before the child spawns and
        describes the whole execution rather than one measurement. Without it a
        byte-exact run and a text run of the same command emit the same
        counters and span attributes, so a timeout, teardown failure, or
        terminal failure cannot be attributed to the mode that hit it.
        ``None`` only on legacy or hand-built events; see
        :data:`~cuprum.events.CaptureMode`.

    A field appended to this declaration preserves an existing positional slot
    only if nothing is inserted *ahead* of that slot: inserting one ahead of
    ``exec_id`` would silently rebind existing positional arguments, handing
    the correlation token to the new field and leaving ``exec_id=None`` — which
    consumers such as ``TracingHook`` treat as uncorrelatable and drop. Nor is
    appending enough by itself, because declaration follows insertion:
    ``terminal_outcome`` is pinned as the tail by ``test_terminal_outcome_public_api``
    yet holds a positional slot of its own, so a later field is declared
    ``kw_only`` and inserted immediately before it. That keeps both the pinned
    tail and every pre-existing slot exactly where they were.

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
    # Declared before ``terminal_outcome`` so that field stays the declaration
    # tail, which ``test_terminal_outcome_public_api`` pins via
    # ``dc.fields()[-1]``. ``terminal_outcome`` already holds a positional slot
    # on the comparison base, so declaring ``resolved_path`` ahead of it
    # positionally would shift that slot and silently rebind an existing
    # caller's positional argument. ``kw_only`` keeps the tail without moving
    # any pre-existing slot; see the class docstring.
    resolved_path: str | None = dc.field(default=None, kw_only=True)
    # ``kw_only`` for the same reason ``resolved_path`` is: the mode is known on
    # every phase, and declaring it positionally ahead of ``terminal_outcome``
    # would shift that pinned field's slot; see the class docstring.
    capture_mode: CaptureMode | None = dc.field(default=None, kw_only=True)
    # The three group-teardown fields are ``kw_only`` for the reason the two
    # above are, and so take the same place: declared immediately *before*
    # ``terminal_outcome`` rather than appended behind it. Appending would move
    # the declaration tail that ``test_terminal_outcome_public_api`` pins at
    # ``dc.fields()[-1]``, while deleting nothing from the positional sequence
    # — so it would trade one invariant away to buy nothing. Either placement
    # leaves ``terminal_outcome`` at the positional slot a released caller
    # binds, because ``kw_only`` fields are absent from that sequence; only
    # this one also leaves it the tail. See the class docstring.
    #
    # ``group_outcome`` labels a bounded counter, so it is typed as the closed
    # enum rather than as a string that a caller could set freely.
    owns_group: bool | None = dc.field(default=None, kw_only=True)
    group_outcome: GroupTeardownOutcome | None = dc.field(default=None, kw_only=True)
    settle_s: float | None = dc.field(default=None, kw_only=True)
    terminal_outcome: TerminalOutcome | None = None
    error_category: StdioFailureCategory | None = None


type ExecHook = cabc.Callable[[ExecEvent], cabc.Awaitable[None] | None]


__all__ = [
    "CaptureMode",
    "ExecEvent",
    "ExecHook",
    "ExecId",
    "ExecPhase",
    "GroupTeardownOutcome",
    "ResourceUsageMode",
    "StdioFailureCategory",
    "TerminalOutcome",
    "TimeoutMode",
    "new_exec_id",
]
