"""Drain the buffered prefix while the native hand-off holds its pair.

The pair is acquired before the first suspension, then held across the
buffered drain, so the drain is the window in which a failure must give the
pair back. This module owns that window and every way it can end, keeping the
release identical on all of them: the pair closes exactly once, the reader
resumes, and the bounded hand-off outcome names what actually happened.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc

from cuprum._pipeline_stream_fds import (
    _close_native_pump_worker_fd,
    _NativePumpWorkerFds,
    _ReaderPause,
    _resume_reader_transport,
)
from cuprum.pump_events import RustPumpHandoffOutcome
from cuprum.pump_observation import (
    _emit_rust_pump_hold,
    _settle_hold_outcome,
)


def _close_unactivated_rust_pump_fds(worker_fds: _NativePumpWorkerFds) -> None:
    """Close an acquired pair that never reached blocking activation."""
    try:
        _close_native_pump_worker_fd(worker_fds.reader_fd)
    finally:
        _close_native_pump_worker_fd(worker_fds.writer_fd)


def _release_unactivated_pair(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    outcome: RustPumpHandoffOutcome,
) -> None:
    """Close the unactivated pair, resume the reader, and settle ``outcome``."""
    _close_unactivated_rust_pump_fds(worker_fds)
    _resume_reader_transport(reader_pause.resume)
    _settle_hold_outcome(outcome)


async def _drain_before_rust_pump_submission(
    worker_fds: _NativePumpWorkerFds,
    reader_pause: _ReaderPause,
    drain: cabc.Callable[[], cabc.Awaitable[None]],
) -> None:
    """Drain the buffered prefix while holding the acquired pair.

    A failed or cancelled drain never falls back: the pair closes, the reader
    resumes, and the error travels. ``drain`` is injected to keep its test seam
    with the coordinator owning the ordering.

    Each failure carries its own bounded outcome, because an operator reading
    the hand-off counter has different questions for each. ``DRAIN_CANCELLED``
    is the caller's own ``CancelledError`` reaching the drain, ``DRAIN_FAILED``
    is an error the drain itself raised, and ``HOLD_OBSERVER_SHUTDOWN`` is a
    shutdown signal from a hold observer raised while the acquisition was being
    published, before the drain began. Publishing that acquisition is a
    separate guarded step: the observer's signal is not a drain failure, and
    reporting it as one would hide the drain never having started.

    Raises
    ------
    asyncio.CancelledError
        When the caller cancels the drain. The pair is released and the
        cancellation re-raised, so the hop settles without a submission.
    """
    try:
        # Publication can itself raise the shutdown signal this guard handles.
        _emit_rust_pump_hold(1)
    except BaseException:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.HOLD_OBSERVER_SHUTDOWN,
        )
        raise
    try:
        await drain()
    except asyncio.CancelledError:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.DRAIN_CANCELLED,
        )
        raise
    except BaseException:
        _release_unactivated_pair(
            worker_fds,
            reader_pause,
            RustPumpHandoffOutcome.DRAIN_FAILED,
        )
        raise
