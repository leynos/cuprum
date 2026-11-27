"""The per-stream live observers a drain can carry.

A byte run's live queue is fed from the ordinary drain. The two callbacks that
feed it travel together as one record, and that record is part of the stream's
configuration rather than a set of extra drain parameters: the tap belongs to
one stream exactly as its capture policy and echo gate do, and bundling it
keeps the drain's signature inside the repository's argument limit.

The type aliases live here rather than in ``cuprum._streams`` so that
``cuprum._stream_config`` can name the tap without importing the drain module,
which would close a cycle.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# A chunk sink observes every raw chunk after capture and echo and before any
# line decoding. A ``None`` answer is a synchronous observer; an awaitable one
# is awaited, which is what lets a live queue apply backpressure.
type _ChunkSink = cabc.Callable[[bytes], cabc.Awaitable[None] | None]
# The end-of-stream hook is synchronous by contract: it runs between the last
# chunk's fan-out and the capture's finalization, and nothing it could suspend
# on belongs to that window.
type _EofSink = cabc.Callable[[], None]


@dc.dataclass(frozen=True, slots=True)
class _StreamTap:
    """One stream's live observers, in the shape the drain accepts."""

    on_chunk: _ChunkSink
    on_eof: _EofSink


__all__ = ["_ChunkSink", "_EofSink", "_StreamTap"]
