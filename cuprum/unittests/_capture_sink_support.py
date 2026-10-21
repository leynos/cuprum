"""Drive a capture sink the way a real drain drives it.

The capture suites test three different accumulators, but they all reach them
through :class:`~cuprum._capture_types.CaptureSink`. Each suite had grown its
own copies of "await one absorb per chunk", "do that on a fresh loop", and
"read the final result", which is three chances for the helpers to disagree
about the one thing they exist to model.

The distinction that matters is the loop boundary. ``absorb`` and ``finalize``
are awaited by the *same* task in a run, so a helper that fed chunks on one
``asyncio.run`` and finalized on another would let a sink pass here that could
not finish a stream for real. :func:`_drain_sink` keeps both phases on one loop
for that reason, and :func:`_absorb` and :func:`_finalize` exist only for the
cases that need to inspect the sink *between* those phases.

The spill suites hand their accumulator a policy rather than a sink, so they
get two more drivers shaped by the same loop boundary. :func:`_spilling_to`
builds the one value the policy is, and :func:`_spill` absorbs and finalizes
a file-backed accumulator on one loop. Both live here rather than in a spill
test module so that the spill suite and its cancellation suite, which each
drive a spill, cannot drift into configuring two different policies.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._capture_file import _FileAccumulator
from cuprum.capture_policy import FileBackedCapture

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum._capture_decode import _DecodeSettings
    from cuprum._capture_types import CaptureFinal, CaptureSink


async def _feed(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> None:
    """Await one absorb per chunk, in the order the drain would read them."""
    for chunk in chunks:
        await sink.absorb(chunk)


def _absorb(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> None:
    """Feed every chunk to *sink* in order, on a loop of its own.

    The drain awaits the sink on a running loop, so a test that called the
    coroutine without one would test nothing. Driving it here keeps the cases
    synchronous, and each call builds a fresh loop — the accumulator is built
    outside one and has to work across that boundary, which is how a config
    reused by a second run reaches it.
    """
    asyncio.run(_feed(sink, chunks))


def _finalize(sink: CaptureSink) -> CaptureFinal:
    """Read *sink*'s result, as a drain that reached the stream's end does."""
    return asyncio.run(sink.finalize())


def _drain_sink(sink: CaptureSink, chunks: cabc.Iterable[bytes]) -> CaptureFinal:
    """Feed *chunks* to *sink*, then read its result, as one drain would.

    One loop for both phases on purpose: the drain awaits ``absorb`` and
    ``finalize`` on the same running loop, and a helper that split them across
    two ``asyncio.run`` calls would let a sink pass here that cannot finish a
    stream in a real run.

    Returns
    -------
    CaptureFinal
        The payload and record the sink reported once the stream ended.
    """

    async def drain() -> CaptureFinal:
        """Absorb the stream, then finalize it."""
        await _feed(sink, chunks)
        return await sink.finalize()

    return asyncio.run(drain())


def _spilling_to(
    directory: pathlib.Path,
    *,
    threshold: int,
    keep_file: bool = False,
) -> FileBackedCapture:
    """Return a file-backed policy spilling into *directory* at *threshold*."""
    return FileBackedCapture(
        spill_threshold_bytes=threshold,
        directory=directory,
        keep_file=keep_file,
    )


def _spill(
    payload: bytes,
    policy: FileBackedCapture,
    *,
    settings: _DecodeSettings,
    chunks: int = 1,
) -> CaptureFinal:
    """Feed *payload* through *policy*'s accumulator in *chunks* pieces.

    The policy is passed rather than its fields because the three of them are
    one value — the library already models them as
    :class:`~cuprum.capture_policy.FileBackedCapture`, and unpacking it here
    only to repack it below would put a second copy of that shape in the test.

    Returns
    -------
    CaptureFinal
        The accumulator's finalized output, holding the file still on disk
        because nothing here cleans it up.
    """
    accumulator = _FileAccumulator(
        policy=policy,
        stream="stdout",
        settings=settings,
    )
    size = -(-len(payload) // chunks)

    async def spill() -> CaptureFinal:
        """Absorb the stream in order, then finalize it on the same loop."""
        for start in range(0, len(payload), size):
            await accumulator.absorb(payload[start : start + size])
        return await accumulator.finalize()

    return asyncio.run(spill())
