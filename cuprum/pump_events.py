"""Structured Rust-pump routing events for observability integrations.

An inter-stage pipe hop may hand its raw descriptors to the Rust stream pump.
Whether a hop took that fast path, and whether a worker cancelled mid-transfer
failed on its way out, are internal routing facts: they never change what a
pipeline returns. They are still the facts an operator asks about when a
deployment has quietly stopped taking the fast path.

This event channel is deliberately separate from :mod:`cuprum.events`. A pump
event is not a command lifecycle event, and :data:`~cuprum.events.ExecPhase` is
a closed set that registered consumers match exhaustively — the metrics adapter
raises ``_UnhandledMetricsPhaseError`` on an unrecognized phase by design. A new
phase would therefore start raising inside consumers that were correct when they
were written. A separate event type, carried on a separate hook registry
(:mod:`cuprum.pump_observation`), means a consumer opts in to pump events by
registering for them, and a consumer that does not is untouched.
"""

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ

from cuprum.events import ExecId


class RustPumpDeclineReason(enum.StrEnum):
    """Why an inter-stage hop fell back from the Rust pump to the Python one.

    A closed set rather than free text. Operators filter on ``cuprum_reason``
    and aggregate the ``reason`` metric label, so a typo at a new call site
    would produce a value their filters silently miss; as an enum it is a type
    error instead. Members are `str`, so the logged field and the metric label
    stay plain strings. ``PLATFORM_UNSUPPORTED`` means the current operating
    system and asyncio transport combination cannot safely use the native Rust
    pump. ``DUPLICATE_FDS_UNAVAILABLE`` means a transport's pipe description
    could not be re-opened or duplicated for the native worker: the
    descriptors existed when extraction read them, but one was already closed
    by the time duplication re-opened it.

    Examples
    --------
    The member value is the string operators see::

        assert RustPumpDeclineReason.RAW_FD_UNAVAILABLE == "raw_fd_unavailable"

    """

    RAW_FD_UNAVAILABLE = "raw_fd_unavailable"
    READER_UNRESUMABLE = "reader_unresumable"
    READER_PAUSE_FAILED = "reader_pause_failed"
    BLOCKING_MODE_UNAVAILABLE = "blocking_mode_unavailable"
    DUPLICATE_FDS_UNAVAILABLE = "duplicate_fds_unavailable"
    PLATFORM_UNSUPPORTED = "platform_unsupported"


class RustPumpHandoffOutcome(enum.StrEnum):
    """A bounded result of attempting the Rust writer-resource hand-off.

    The three outcomes around the buffered drain stay distinct so that an
    operator reading `cuprum_rust_pump_handoff_total` can tell what the drain
    did. ``DRAIN_FAILED`` is an error the drain itself raised,
    ``DRAIN_CANCELLED`` is the caller's cancellation reaching the drain, and
    ``HOLD_OBSERVER_SHUTDOWN`` is a shutdown signal from a hold observer raised
    while the acquired pair was being published, before the drain began. All
    three release the acquired pair, resume the reader, and re-raise the
    original exception, so the hop settles without a submission. A pump
    observer that raises a shutdown signal while one of these outcomes is
    emitted takes precedence over that re-raise, because a shutdown signal must
    still travel; the original error remains reachable through the raised
    signal's exception context chain.

    ``SUBMISSION_ABANDONED`` covers the same observer problem one step later:
    the drain finished and blocking activation ran, but a hold observer raised
    a shutdown signal before the pair was submitted. The activated pair is
    rolled back and the signal propagates, so the hop settles without a
    submission and without leaving an open, blocking pair behind.
    """

    SUBMITTED = "submitted"
    DUPLICATE_WRITER_FAILED = "duplicate_writer_failed"
    BLOCKING_SETUP_FAILED = "blocking_setup_failed"
    DRAIN_FAILED = "drain_failed"
    DRAIN_CANCELLED = "drain_cancelled"
    HOLD_OBSERVER_SHUTDOWN = "hold_observer_shutdown"
    SUBMISSION_ABANDONED = "submission_abandoned"
    EXECUTOR_SUBMISSION_REJECTED = "executor_submission_rejected"
    NATIVE_LOAD_FAILED = "native_load_failed"
    READER_PREPARATION_FAILED = "reader_preparation_failed"
    BUFFER_VALIDATION_FAILED = "buffer_validation_failed"
    PLATFORM_WRITER_TRANSFER_FAILED = "platform_writer_transfer_failed"
    WINDOWS_RAW_HANDLE_REJECTED = "windows_raw_handle_rejected"
    NATIVE_IO_FAILED = "native_io_failed"


type PumpPhase = typ.Literal[
    "declined",
    "handoff",
    "handoff_hold",
    "failed_after_cancel",
    "cleanup_started",
    "cleanup_completed",
    "cleanup_grace_expired",
    "cleanup_deferred",
]
"""What a :class:`PumpEvent` reports.

``declined``
    A hop fell back to the Python pump; ``reason`` names the seam that refused.
``handoff``
    A Rust writer-resource hand-off reached one of the closed ``outcome``
    states.
``handoff_hold``
    The hand-off took its acquired descriptor pair into, or handed it back
    from, the coordinator's hold around the buffered drain. ``held_delta`` is
    ``+1`` on acquisition and ``-1`` on release, so a consumer that accumulates
    the deltas reports the pairs currently held across pending drains.
``failed_after_cancel``
    A cancelled hop's Rust worker had failed, and the failure was consumed
    rather than resurfacing detached at garbage collection.
``cleanup_started``
    Cancellation began waiting for the native worker's descriptor cleanup.
``cleanup_completed``
    The native worker released its descriptors after cancellation; ``duration_s``
    records the monotonic cleanup wait.
``cleanup_grace_expired``
    The caller's bounded cleanup grace elapsed; ``elapsed_s`` records the
    monotonic grace wait before callback-owned cleanup was deferred.
``cleanup_deferred``
    A worker that outlived its caller-facing grace later completed its deferred
    descriptor cleanup.
"""


# ``eq=False`` keeps identity comparison: two open emissions are never
# interchangeable, and the consumer that deduplicates deliveries compares the
# tokens with ``is``.
@dc.dataclass(slots=True, eq=False)
class PumpEmission:
    """The identity of one pump event's delivery to the registered hooks.

    ``_emit_pump_event`` stamps every event it delivers with one of these, and
    closes it when every hook has seen that event. A consumer that must act
    once per emission — the unlabelled held-pair gauge, which several hooks
    reporting to one collector all move — can therefore recognize the other
    deliveries of the same emission by identity, and tell them apart from a new
    emission that merely carries the same values.

    The token is the emitter's public statement about what is in flight, so a
    consumer never has to read the emitter's own bookkeeping. It is the only
    mutable part of a pump event: the event stays frozen, and only the emitter
    calls :meth:`close`.

    Examples
    --------
    An event delivered to a hook carries a token that is open until delivery
    ends::

        def record(event):
            assert event.emission is not None
            assert not event.emission.is_closed

    """

    _is_closed: bool = dc.field(default=False, init=False)

    @property
    def is_closed(self) -> bool:
        """Whether the emitter has finished delivering this emission."""
        return self._is_closed

    def close(self) -> None:
        """Mark the delivery finished.

        Called by the emitter once every hook has seen the event. A consumer
        reads :attr:`is_closed`; it must not close a token it was handed,
        because the remaining deliveries of that emission would then look like
        a new emission and move the count twice.
        """
        self._is_closed = True


@dc.dataclass(frozen=True, slots=True)
class PumpEvent:
    """A Rust-pump routing decision reported to registered pump hooks.

    The event carries the phase, a closed-set reason or hand-off outcome, the
    monotonic wait duration for completed native-pump cleanup, the monotonic
    elapsed grace wait, and the source stage token needed to correlate cleanup
    tracing. Descriptor numbers,
    command arguments, exception types, and tracebacks are all either
    unbounded as metric labels or a disclosure risk, and the DEBUG log records
    emitted alongside these events carry the diagnostic detail — including the
    pump's traceback — for the cases that need it.

    Attributes
    ----------
    phase:
        Which routing decision this event reports. See :data:`PumpPhase`.
    reason:
        For ``declined``, the seam that refused the hand-off. ``None`` for
        every other phase.
    outcome:
        For ``handoff``, the closed outcome of the writer-resource hand-off.
        ``None`` for every other phase.
    held_delta:
        For ``handoff_hold``, ``+1`` when the coordinator takes the acquired
        pair and ``-1`` when it releases it. ``0`` for every other phase.
    duration_s:
        Monotonic seconds spent waiting for the native worker's cleanup when
        ``phase`` is ``cleanup_completed``. ``None`` for every other phase.
    elapsed_s:
        Monotonic seconds spent waiting before ``cleanup_grace_expired``.
        ``None`` for every other phase.
    exec_id:
        The upstream pipeline stage's stable execution token for native-cleanup
        phases. Tracing observers use it only to locate an existing execution
        span; it is ``None`` for every other phase and is never a trace
        attribute or metric label.
    emission:
        The token the emitter stamps onto every event it delivers, or ``None``
        for an event a caller built and handed to a hook directly. It identifies
        one delivery to the registered hooks, which is what lets a consumer that
        counts emissions recognize the same emission reaching several hooks. It
        is excluded from equality and from the representation: two events with
        the same phase are the same routing fact however they were delivered.

    Examples
    --------
    A decline names the seam that refused::

        event = PumpEvent(
            phase="declined",
            reason=RustPumpDeclineReason.READER_PAUSE_FAILED,
        )
        assert event.reason == "reader_pause_failed"

    A cancellation-masked failure carries no reason::

        assert PumpEvent(phase="failed_after_cancel").reason is None

    A completed cleanup carries its monotonic duration::

        cleanup = PumpEvent(phase="cleanup_completed", duration_s=0.25)
        assert cleanup.duration_s == 0.25

    A hold reports the acquired pair as it is taken and released::

        taken = PumpEvent(phase="handoff_hold", held_delta=1)
        assert taken.held_delta == 1

    """

    phase: PumpPhase
    reason: RustPumpDeclineReason | None = None
    outcome: RustPumpHandoffOutcome | None = None
    duration_s: float | None = None
    elapsed_s: float | None = None
    exec_id: ExecId | None = None
    # Appended last so adding it cannot shift the positional fields callers
    # already passed to this public dataclass.
    held_delta: int = 0
    # Delivery identity rather than routing fact: excluded from comparison so
    # that an event a hook compares against a hand-built one still matches.
    emission: PumpEmission | None = dc.field(default=None, compare=False, repr=False)


type PumpHook = cabc.Callable[[PumpEvent], None]
"""A synchronous consumer of :class:`PumpEvent` values.

Pump hooks are synchronous by contract. Every emission site is synchronous —
one on the Python-pump fall-back path and the others inside cancellation
unwinding — so there is no task to attach an awaitable to and no point at which
awaiting it could be ordered against the transfer it describes. A hook that
returns an awaitable is reported and discarded rather than run.
"""


__all__ = [
    "PumpEmission",
    "PumpEvent",
    "PumpHook",
    "PumpPhase",
    "RustPumpDeclineReason",
    "RustPumpHandoffOutcome",
]
