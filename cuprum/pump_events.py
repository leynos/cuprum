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

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
import enum
import typing as typ


class RustPumpDeclineReason(enum.StrEnum):
    """Why an inter-stage hop fell back from the Rust pump to the Python one.

    A closed set rather than free text. Operators filter on ``cuprum_reason``
    and aggregate the ``reason`` metric label, so a typo at a new call site
    would produce a value their filters silently miss; as an enum it is a type
    error instead. Members are `str`, so the logged field and the metric label
    stay plain strings.

    Examples
    --------
    The member value is the string operators see::

        assert RustPumpDeclineReason.RAW_FD_UNAVAILABLE == "raw_fd_unavailable"

    """

    RAW_FD_UNAVAILABLE = "raw_fd_unavailable"
    READER_UNRESUMABLE = "reader_unresumable"
    READER_PAUSE_FAILED = "reader_pause_failed"
    BLOCKING_MODE_UNAVAILABLE = "blocking_mode_unavailable"


type PumpPhase = typ.Literal["declined", "failed_after_cancel"]
"""What a :class:`PumpEvent` reports.

``declined``
    A hop fell back to the Python pump; ``reason`` names the seam that refused.
``failed_after_cancel``
    A cancelled hop's Rust worker had failed, and the failure was consumed
    rather than resurfacing detached at garbage collection.
"""


@dc.dataclass(frozen=True, slots=True)
class PumpEvent:
    """A Rust-pump routing decision reported to registered pump hooks.

    The event carries the phase and, for a decline, the closed-set reason. It
    deliberately carries nothing else. Descriptor numbers, command arguments,
    exception types, and tracebacks are all either unbounded as metric labels
    or a disclosure risk, and the DEBUG log records emitted alongside these
    events already carry the diagnostic detail — including the pump's
    traceback — for the cases that need it.

    Attributes
    ----------
    phase:
        Which routing decision this event reports. See :data:`PumpPhase`.
    reason:
        For ``declined``, the seam that refused the hand-off. ``None`` for
        every other phase.

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

    """

    phase: PumpPhase
    reason: RustPumpDeclineReason | None = None


type PumpHook = cabc.Callable[[PumpEvent], None]
"""A synchronous consumer of :class:`PumpEvent` values.

Pump hooks are synchronous by contract. Both emission sites are synchronous
functions — one on the Python-pump fall-back path, one inside cancellation
unwinding — so there is no task to attach an awaitable to and no point at which
awaiting it could be ordered against the transfer it describes. A hook that
returns an awaitable is reported and discarded rather than run.
"""


__all__ = [
    "PumpEvent",
    "PumpHook",
    "PumpPhase",
    "RustPumpDeclineReason",
]
