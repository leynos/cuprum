"""Shared support for tests that drive the stream drains.

Both drain modes share one entry point, so a test-local coroutine that awaits
it receives a whole :class:`~cuprum._stream_drain_finish._CapturedDrain` —
the widened ``str | bytes`` payload plus the record and spill handle beside
it. A helper that reports *what it captured* has two honest shapes, and the
aliases here name both rather than one: a helper that reads ``.payload`` off
the drain and reports that reports a payload, while one that hands the drain
on untouched reports the drain. Collapsing the two into a single alias is
what let thirteen annotations drift from the values they described.

The two capture configurations are here for the same reason: every drain test
has to say which of the run's two streams, if either, is being preserved, and a
test that pastes its own pair reads as though its two policies meant something
distinct. They do not — only *whether* a stream is captured is ever intended,
and two names say that once.
"""

from __future__ import annotations

from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._subprocess_wait_types import _StreamConsumerTask
from cuprum.capture_policy import FullCapture

# One drain's captured payload: text in the ordinary mode, the child's bytes
# untouched in the byte-exact one.
type CapturedPayload = str | bytes
# A payload a helper reports, or ``None`` for a drain that retained nothing.
type CapturedOrNone = CapturedPayload | None
# One stream's payload paired with another value the helper also reports.
type CapturedPair[T] = tuple[CapturedOrNone, T]

# A settled drain, record and handle included: what ``_drain`` and
# ``_consume_stream`` hand back, and so what a helper that forwards their
# result unchanged reports. Separate from ``CapturedOrNone`` because the two
# are no longer interchangeable — the drain *contains* a payload, it is not
# one, and a helper annotated as returning a payload cannot forward it.
type CapturedDrain = _CapturedDrain
# One settled drain paired with another value the helper also reports.
type CapturedDrainPair[T] = tuple[CapturedDrain, T]

# A reader the drain owns. Named so a test double standing in for the drain
# -- an injected EOF-grace waiter, or the drain itself -- can match the
# signature without respelling the task's payload type.
type ConsumerTask = _StreamConsumerTask


def captured(payload: CapturedPayload | None) -> _CapturedDrain:
    """Report ``payload`` as a settled reader would, with no record beside it.

    A reader settles to a whole :class:`_CapturedDrain`, not a bare payload, so
    a consumer double has to say what it captured within that shape. The
    record is deliberately left unset: these doubles stand in for a reader
    whose capture the *drain* is about, and a double that invented a
    :class:`~cuprum.capture_policy.CapturedStream` would answer a question the
    test is not asking — most visibly in the non-capturing contract, where the
    absence of a record is half of what is being asserted.

    Parameters
    ----------
    payload : CapturedPayload | None
        What the reader captured, or ``None`` for a reader that captured
        nothing at all.

    Returns
    -------
    _CapturedDrain
        The result the drain will report for this reader.

    Examples
    --------
    A consumer double returns one of these rather than a bare string::

        async def consumer() -> _CapturedDrain:
            return captured("stderr")
    """
    return _CapturedDrain(payload=payload)


# The ``(stdout, stderr)`` capture pair for a run that preserves both streams,
# and for one that preserves neither. ``FullCapture`` rather than a bounded or
# file-backed policy on purpose: these tests are about the drain's behaviour —
# which readers it settles, what it reports, and what it records — and the
# retention rules of a *policy* are the capture-policy suites' subject. A
# ``None`` entry is not an unset policy but the absence of one: it is what a
# route reports when output is discarded, and it is the only way to say that
# per stream.
_BOTH_CAPTURED: tuple[FullCapture, FullCapture] = (FullCapture(), FullCapture())
_NONE_CAPTURED: tuple[None, None] = (None, None)

__all__ = [
    "_BOTH_CAPTURED",
    "_NONE_CAPTURED",
    "CapturedDrain",
    "CapturedDrainPair",
    "CapturedOrNone",
    "CapturedPair",
    "CapturedPayload",
    "ConsumerTask",
    "captured",
]
