"""How one stream drain reports what it captured.

Split from ``cuprum._streams``, which owns the read/echo/buffer loop. This is
the *result* half of that loop: what a drain hands back once its reads stop,
whether they stopped at EOF or at a cancellation, and the single seam that
decides between the child's own bytes and decoded text.

Keeping the two apart is what makes the mode guarantee reviewable. Both exits
route through one payload renderer here, so a cancelled read and a completed
one cannot disagree about the type they return — an asymmetry a caller could
only discover by timing the cancellation — and each exit names its own
:class:`~cuprum.stream_events.StreamOperationOutcome` exactly once.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._stream_echo import _flush_echo_decoder
from cuprum.capture_policy import CapturedStream, policy_kind
from cuprum.stream_events import StreamOperationOutcome
from cuprum.stream_observation import _complete_stream_operation

if typ.TYPE_CHECKING:
    from cuprum._capture_types import SpillHandle
    from cuprum._streams import _DrainState, _StreamConfig
    from cuprum.capture_policy import CapturePolicy, CaptureStreamName
    from cuprum.stream_observation import _StreamOperationMeasurement


async def _finish_drain(
    state: _DrainState,
    measurement: _StreamOperationMeasurement | None,
    *,
    reached_eof: bool,
) -> _CapturedDrain:
    """Complete one drain and return whatever it captured.

    A cancelled read is not a failure to capture: the bytes already read are
    still the child's output, so the partial prefix is finalized under the same
    rule as a completed drain. Only the two conditions below — no capture
    accumulator, or a caller-requested discard — turn it into a cancellation.

    Finalizing is awaited because a file-backed capture's last act is to flush
    its spill file and, in byte mode, read it back — both on a worker. See
    :class:`~cuprum._capture_types.CaptureSink`.

    Returns
    -------
    _CapturedDrain
        The drain's payload and the record describing it. Both are ``None``
        when this drain was not capturing; a recording drain always reports a
        capture, even when its payload is an empty string or byte string.

    Raises
    ------
    asyncio.CancelledError
        If the reads stopped short of EOF, this drain kept a capture
        accumulator, and the caller did not ask for a cancellation to discard
        it.
    """
    if not reached_eof:
        _complete_stream_operation(measurement, StreamOperationOutcome.CANCELLED)
        if state.capture is None or _discard_on_cancel(state.config):
            raise asyncio.CancelledError
        _flush_echo_decoder(state)
        return await _finalize(state)
    _flush_echo_decoder(state)
    captured = await _finalize(state) if state.capture is not None else _CapturedDrain()
    _complete_stream_operation(measurement, StreamOperationOutcome.EOF)
    return captured


@dc.dataclass(frozen=True, slots=True)
class _CapturedDrain:
    """What one drain finalized: the payload, and what it says about itself.

    Returned together rather than as two member reads, because a caller must
    take both or neither: a capture record without its payload would describe
    text nobody received, and a payload without its record is the pre-policy
    behaviour that could not tell a truncated capture from a short one.

    ``handle`` travels with them for the same reason. A spilled capture leaves
    a file on disk and the finalize result is the only thing that knows its
    path, so a drain that dropped the handle here would hand the caller a
    record naming a file nobody could remove. It is also how the *normal* path
    closes the file: :func:`_finalize_drain_handles` reads it off this record
    at the result-construction site, while the teardown path closes the same
    file through :meth:`cuprum._capture_types.CaptureSink.cleanup`.
    """

    payload: str | bytes | None = None
    capture: CapturedStream | None = None
    handle: SpillHandle | None = None

    def parts(self) -> _DrainParts:
        """Report the three pieces separately, in their documented order.

        Callers that carry the pieces inside a wider record of their own use
        this rather than the fields, so the order is named once and a caller
        that rebuilt the triple itself could not get it wrong.

        Returns
        -------
        _DrainParts
            The payload, capture record, and spill handle, in that order.
        """
        return self.payload, self.capture, self.handle


# The payload, record, and handle as an ordered triple, for the records that
# carry them without being a drain result. Named rather than spelled inline so
# a reader of those records does not have to work out from a three-member tuple
# annotation which member is which.
type _DrainParts = tuple[
    str | bytes | None,
    CapturedStream | None,
    SpillHandle | None,
]


async def _finalize(state: _DrainState) -> _CapturedDrain:
    """Finalize the drain's accumulator, if it had one.

    Returns
    -------
    _CapturedDrain
        The accumulator's payload, record, and handle; an empty result when
        this drain had no accumulator to finalize.
    """
    sink = state.capture
    if sink is None:
        return _CapturedDrain()
    final = await sink.finalize()
    return _CapturedDrain(
        payload=final.payload,
        capture=final.capture,
        handle=final.handle,
    )


def _empty_capture_for(
    *,
    policy: CapturePolicy | None,
    stream: CaptureStreamName,
    capture_bytes: bool,
) -> _CapturedDrain:
    """Report an unattached stream as the capture its policy would have made.

    A stream that was never attached is not absent output: the run did capture,
    and the child simply had no such pipe. Reporting the record as well as the
    empty payload is what keeps this branch indistinguishable from a real
    drain of a stream that held nothing — the alternative, a bare empty string
    beside a missing record, is the shape that made the two paths disagree
    about their type and surfaced as an execution-invariant error.

    Returns
    -------
    _CapturedDrain
        The empty payload in the run's mode, with the capture record when a
        policy was configured; an empty result when the stream was not being
        captured at all.
    """
    if policy is None:
        return _CapturedDrain()
    payload = b"" if capture_bytes else ""
    return _CapturedDrain(
        payload=payload,
        capture=CapturedStream(
            stream=stream,
            policy=policy_kind(policy),
            total_bytes=0,
        ),
    )


def _empty_capture(config: _StreamConfig) -> _CapturedDrain:
    """Report an unattached stream in the mode the run asked for."""
    return _empty_capture_for(
        policy=config.capture,
        stream=config.stream.value,
        capture_bytes=config.capture_bytes,
    )


async def _finalize_drain_handles(*results: _CapturedDrain | None) -> None:
    """Close and remove every spill file the given drain results still own.

    This is the *normal* path's half of the cleanup contract, and it runs from
    the result-construction sites rather than from teardown. A spill file is
    normally removed by the run that created it, before that run returns: the
    caller receives a record whose ``path`` names a file that is already gone,
    which is the behaviour the implementation's decision log settled on after
    the plan left the lifetime open, and it means a long-lived process cannot
    accumulate spill files across many runs.

    ``keep_file=True`` is the whole of the exception: the caller asked for the
    file, so this closes the descriptor and leaves the file alone.

    It runs under the caller's shield — every caller is a teardown or result
    path that is already shielding its cleanup — so a cancellation landing
    here cannot leave a half-removed file behind.

    Parameters
    ----------
    *results : _CapturedDrain | None
        The drain results whose handles are still owned, in any order. A
        result with no handle is skipped, which is every result of a
        memory-backed capture. ``None`` is skipped too: a pipeline stage whose
        stream was never consumed has no drain at all, and the callers that
        gather per-stage results hand those through rather than filtering them,
        so one loop reads one shape.

    Examples
    --------
    Closing is idempotent, so a run that removed its own file and a teardown
    that also reached the sink agree rather than raising::

        await _finalize_drain_handles(drained)
    """
    for result in results:
        handle = None if result is None else result.handle
        if handle is not None:
            await handle.close(remove=True)


def _discard_on_cancel(config: _StreamConfig) -> bool:
    """Whether cancellation must discard buffered bytes without decoding them."""
    return config.discard_on_cancel is not None and config.discard_on_cancel.is_set()


__all__ = [
    "_CapturedDrain",
    "_DrainParts",
    "_discard_on_cancel",
    "_empty_capture",
    "_empty_capture_for",
    "_finalize_drain_handles",
    "_finish_drain",
]
