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
"""

from __future__ import annotations

import asyncio
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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
