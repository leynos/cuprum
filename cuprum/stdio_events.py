"""Bounded categories for standard-stream failure diagnostics.

The one type here names *which* standard-stream boundary failed, rather than
*what exception type* it raised. It lives apart from :mod:`cuprum.events`, which
is already at the project's module-size ceiling and whose ``ExecPhase`` it
labels, so the label is imported the same way ``cuprum.pump_events`` supplies
its own bounded enums.

:class:`StdioFailureCategory` is re-exported from :mod:`cuprum.events` for
callers that already import that surface, so the two spellings name the same
class.
"""

import enum


# The stable ``error_category`` values, naming *which* standard-stream boundary
# failed rather than *what exception type* it raised.
#
# The distinction is load-bearing because several boundaries raise the same
# exception. Advancing a producer and writing to the child's pipe both raise
# ``OSError`` in the pipe family, an invalid chunk and a mistyped encoder
# both raise ``TypeError``, and a producer's own ``BrokenPipeError`` is
# indistinguishable at the type level from the child closing its end. An
# operator told only ``OSError`` cannot say whether their producer broke or
# their child stopped reading, and those call for opposite responses.
#
# It is a closed set for the same reason ``ResourceUsageMode`` is: the value is
# a metric label an operator filters on, so a typo at a new call site would
# produce a value their filters silently miss. As an enum it is a type error
# instead. It carries no path, payload, argv, or exception text: the fields it
# labels are the ones safe to retain in an operator's series.
class StdioFailureCategory(enum.StrEnum):
    """Which standard-stream boundary produced a diagnostic failure.

    Members are `str`, so the observe event, the ``cuprum_`` log extras, the
    span attributes, and the metrics label all carry plain strings.

    Examples
    --------
    The member value is the string consumers see::

        assert StdioFailureCategory.INVALID_CHUNK == "invalid_chunk"

    """

    #: Advancing the caller's producer raised. The producer failed, not the
    #: child, even when the exception it raised is a pipe error.
    PRODUCER = "producer"
    #: The producer yielded something other than ``str`` or ``bytes``.
    INVALID_CHUNK = "invalid_chunk"
    #: Building or driving the run's incremental encoder failed.
    ENCODER = "encoder"
    #: A pipe write failed for a reason other than the child closing its end.
    #: The child closing early is not a failure and keeps its existing
    #: ``stdin_error`` diagnostic rather than being counted here.
    PIPE = "pipe"
    #: Opening a cuprum-owned target file, immediately before the spawn.
    OWNED_PATH_OPEN = "owned_path_open"
    #: Flushing a caller's borrowed file object, immediately before the spawn.
    BORROWED_FLUSH = "borrowed_flush"


__all__ = ["StdioFailureCategory"]
