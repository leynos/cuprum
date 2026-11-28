"""The failure a run reports when an output consumer ends cancelled.

A stream consumer that is cancelled while the run is still live leaves its
pipe undrained: the child keeps writing, fills the pipe, and blocks rather than
exiting. The supervisor has to end the run on that outcome, but it cannot
re-raise the consumer's own :class:`asyncio.CancelledError`. That type is
indistinguishable from the caller's own cancellation, so
``except asyncio.CancelledError`` would swallow it, ``asyncio.timeout`` would
read it as its own expiry, and a caller could never tell a cancelled consumer
from a cancel it requested.

This error is the distinguishable spelling of that outcome. It is an ordinary
:class:`Exception` carrying the stream whose consumer stopped, so a caller can
branch on it, log it, and still receive a plain ``CancelledError`` when it
cancels the run itself.

It lives in its own leaf module because ``cuprum.sh`` re-exports it: the
package's public exceptions are importable from the facade even though their
defining module is private, the same arrangement
:mod:`cuprum._process_handle_errors` gives ``SignalAfterExitError``. The module
imports nothing but the standard library, so the stream naming vocabulary is
declared here and cross-checked against
:data:`cuprum.lines.LineStreamName` in the public-surface tests rather than
imported, which would make this a coupling to the whole ``cuprum`` package.
"""

from __future__ import annotations

import typing as typ

# The two streams a run observes. Declared here rather than imported from
# ``cuprum.lines`` so this module stays a leaf; the public-surface tests pin
# that it is structurally the same literal.
type ConsumerStreamName = typ.Literal["stdout", "stderr"]

__all__ = ["ConsumerCancelledError", "ConsumerStreamName"]


class ConsumerCancelledError(Exception):
    """Raised when a stream consumer ended cancelled before end of stream.

    The supervisor raises this instead of the consumer's own
    :class:`asyncio.CancelledError`, so the caller can tell an output consumer
    that stopped — a callback that raised ``CancelledError``, or a consumer
    task cancelled from outside — from a cancellation the caller itself
    requested. The child has been terminated and reaped by the time this
    reaches the caller.

    Attributes
    ----------
    stream : ConsumerStreamName
        Which stream's consumer ended cancelled: ``"stdout"`` or ``"stderr"``.

    """

    stream: ConsumerStreamName

    def __init__(self, stream: ConsumerStreamName) -> None:
        """Record the stream whose consumer ended cancelled."""
        self.stream = stream
        super().__init__(f"{stream} consumer ended cancelled before end of stream")
