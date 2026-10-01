"""Shared type aliases for tests that drive the stream drains.

Both drain modes share one entry point, so a test-local coroutine that awaits
it receives the widened ``str | bytes`` payload rather than text. The aliases
here name what such a helper returns, once, instead of leaving every helper
annotation in the suite to spell the same union and drift from it.
"""

from __future__ import annotations

from cuprum._subprocess_wait_types import _StreamConsumerTask

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

__all__ = ["CapturedOrNone", "CapturedPair", "CapturedPayload", "ConsumerTask"]
