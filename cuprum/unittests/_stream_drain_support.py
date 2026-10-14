"""Shared support for tests that drive the stream drains.

Both drain modes share one entry point, so a test-local coroutine that awaits
it receives the widened ``str | bytes`` payload rather than text. The aliases
here name what such a helper returns, once, instead of leaving every helper
annotation in the suite to spell the same union and drift from it.

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
# What a test helper returns when it reports a capture and nothing else.
type CapturedOrNone = CapturedPayload | None
# One stream's payload paired with another value the helper also reports.
type CapturedPair[T] = tuple[CapturedOrNone, T]

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
    "CapturedOrNone",
    "CapturedPair",
    "CapturedPayload",
    "ConsumerTask",
    "captured",
]
