"""The refusals a ``wait4``-owning backend raises before it can publish an exit.

The direct-command path has two ways to find itself unable to do what it was
asked, and each is a distinct fact rather than a variation on one. A delivery
can name a child that can no longer be waited for, and a collection can return
a child this owner did not spawn — the second being how a foreign reaper is
detected before its status is mistaken for this command's. Neither is a
:class:`~cuprum._process_handle_errors.SignalAfterExitError`, which is the
*handle's* verdict and is built from a code this layer alone cannot supply.

They live in their own leaf module because they are the backend's error
vocabulary rather than part of its machinery: the reaping loop, the settlement
lock, and the spawn path are one subject, while what this layer refuses and why
is another that happens to be raised from the same file. Keeping the two apart
is also what lets the module below stay inside the repository's 400-line cap
without folding a docstring down to fit, which would have cost the reasoning
the refusals depend on. The arrangement mirrors
:mod:`cuprum._process_handle_errors`, which does the same job for the layer
above.

"""

from __future__ import annotations

from cuprum._pipeline_types import _ExecutionInvariantError


class _DeliveryRefusedError(ProcessLookupError):
    """Raised when the child a delivery names can no longer be waited for.

    This is the backend's own refusal, and it is deliberately separate from the
    handle's :class:`~cuprum._process_handle_errors.SignalAfterExitError`: this
    layer knows only that no waitable child remains, not how the child ended.
    Attaching an exit code here would mean inventing one, because the code — in
    the cases where one can still be established at all — belongs to whoever
    completed the collection, one layer up.

    Subclasses :class:`ProcessLookupError` for the same reason the public error
    does: that is what the kernel reports for the same condition, so a caller
    that already tolerates a lost process keeps working unchanged.
    """

    def __init__(self, pid: int) -> None:
        """Record the child that could not be signalled."""
        super().__init__(f"cannot signal PID {pid}: it is already gone")
        self.pid = pid


class _Wait4InvariantError(_ExecutionInvariantError):
    """Raised when ``wait4`` reports a child this owner did not spawn.

    A mismatch means some other code reaped this child, so the resource usage
    the direct path is about to publish belongs to a different process. Both
    identifiers are exposed as attributes so a caller can tell this failure
    apart from the other invariant failures without parsing the message.
    """

    def __init__(self, expected_pid: int, reaped_pid: int) -> None:
        """Record the two identifiers and build the preserved diagnostic."""
        msg = f"wait4 reaped unexpected child {reaped_pid}, expected {expected_pid}"
        super().__init__(msg)
        self.expected_pid = expected_pid
        self.reaped_pid = reaped_pid


__all__ = [
    "_DeliveryRefusedError",
    "_Wait4InvariantError",
]
