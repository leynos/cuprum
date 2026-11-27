"""Public vocabulary for consuming a running command's streams as bytes.

``SafeCmd.start_bytes()`` hands back a :class:`~cuprum.sh.RunningBytesCommand`
whose ``stdout`` and ``stderr`` are independent readers. Each reader yields
:class:`ByteChunk` values and reaches its own terminal :class:`StreamClosure`.

The types here are the stable part of that contract. The queue, the reader, and
the owned handle live in private modules because their implementations are
free to change; a caller imports ``ByteChunk`` and ``StreamClosure`` from
``cuprum`` or ``cuprum.sh`` and reads the chunks it is given.

Per-stream order is guaranteed. There is no total cross-stream order: the two
streams are drained independently, and a chunk boundary is the parent's read
boundary rather than the child's write boundary.
"""

import dataclasses as dc
import enum

from cuprum.echo_events import EchoStream

__all__ = ["ByteChunk", "StreamClosure"]


class StreamClosure(enum.StrEnum):
    """How one live stream ended, once it has ended.

    A reader reports ``None`` while it is live and exactly one member once its
    terminal state is published. Normal terminal states drain any queued items
    before iteration stops; :attr:`FAILED` takes precedence over pending items
    and is raised as :class:`~cuprum.sh.LiveStreamError` instead.

    Attributes
    ----------
    EOF:
        The drain observed the stream's end. Every byte the child wrote reached
        the reader's queue, and nothing was discarded before the end.
    READER_CLOSED:
        The consumer closed the reader. Queued items are discarded, and later
        live bytes are dropped from the reader while capture continues.
    TRUNCATED_BY_SETTLEMENT:
        Settlement began before the reader reached a clean end. Bytes that did
        not fit the queue afterwards exist only in the run's capture.
    FAILED:
        The drain or the supervisor failed. Iteration and ``wait_eof()`` raise
        a chained ``LiveStreamError``.

    Examples
    --------
    >>> StreamClosure.EOF.value
    'eof'
    >>> StreamClosure.TRUNCATED_BY_SETTLEMENT.value
    'truncated_by_settlement'
    """

    EOF = "eof"
    READER_CLOSED = "reader_closed"
    TRUNCATED_BY_SETTLEMENT = "truncated_by_settlement"
    FAILED = "failed"


@dc.dataclass(frozen=True, slots=True)
class ByteChunk:
    r"""One chunk of a running command's output, as the child's own bytes.

    Attributes
    ----------
    stream:
        Which of the child's streams produced this chunk.
    data:
        The bytes the parent read. They are passed through untouched, so
        malformed UTF-8, split multibyte characters, and protocol records
        without a trailing newline all survive.
    sequence:
        A zero-based counter for this stream only. It lets a consumer assert
        per-stream order without depending on read sizes or timing.

    Examples
    --------
    >>> chunk = ByteChunk(EchoStream.STDOUT, b"\xff\xfe", 0)
    >>> chunk.stream.value
    'stdout'
    >>> chunk.data
    b'\xff\xfe'
    """

    stream: EchoStream
    data: bytes
    sequence: int

    def __post_init__(self) -> None:
        """Reject a payload or sequence the contract cannot represent."""
        if not isinstance(self.data, bytes):
            msg = "ByteChunk data must be bytes"
            raise TypeError(msg)
        if self.sequence < 0:
            msg = "ByteChunk sequence must be non-negative"
            raise ValueError(msg)
