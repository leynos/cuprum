"""Publishing a live run task's outcome to its observers.

A stream consumer and the stdin writer both end on their own schedule, and a
live byte handle must learn about a failure without waiting for settlement to
start. The two callbacks here are the done-callback half of that contract: a
consumer publishes to its reader, while the writer asks for the child to be
stopped and leaves its `StdinSourceError` for settlement.

The name avoids ``_subprocess_task_outcomes``, which the subprocess
supervisor already owns for its own, different task-outcome questions.
"""

from __future__ import annotations

import typing as typ

from cuprum._subprocess_rendezvous import _stdin_writer_failure

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._stream_live import _LiveStreamSink, _StopRequest

__all__ = ["_publish_consumer_outcome", "_publish_writer_failure"]


def _publish_consumer_outcome(
    tap: _LiveStreamSink,
    task: asyncio.Task[typ.Any],
) -> None:
    """Publish a consumer task's failure, or confirm the end it observed.

    A failed task reports ``FAILED`` immediately, so a reader is never left
    waiting for settlement to notice it. A successful task confirms the end it
    noted during its final read, which is what keeps a finalization failure
    from following a reader-visible EOF. Cancellation is teardown's ordinary
    stop and publishes nothing.
    """
    error = _stdin_writer_failure(task)
    if error is not None:
        if tap.on_failure is not None:
            tap.on_failure(error)
        return
    if tap.on_complete is not None:
        tap.on_complete()


def _publish_writer_failure(
    stop: _StopRequest,
    task: asyncio.Task[None],
) -> None:
    """Request a stop when the stdin writer ended with a failure.

    Cancellation is the run's own teardown and requests nothing. The writer has
    already published its error to the log settlement races, so this adds the
    held-handle half: a child blocked on a pipe the failed writer left behind.
    """
    error = _stdin_writer_failure(task)
    if error is not None:
        stop.request(error)
