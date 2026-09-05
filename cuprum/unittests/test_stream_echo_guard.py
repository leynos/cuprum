"""Direct coverage for the echo-encode failure guard in the drain loop.

The canonical drain contract lives in ``test_stream_drain.py``; this module
owns the narrow behaviour added for issue #348: a text-only sink whose
encoding cannot represent the subprocess output disables echo for its drain
while capture completes, exactly one structured warning is logged, other I/O
errors still propagate, and the final decoder flush never re-attempts a
rejected sink.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum._streams import _drain, _StreamConfig

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _Cp1252TextOnlySink:
    """Text-only sink modelling a parent stream too narrow for the output."""

    def __init__(self) -> None:
        """Record each attempted write payload."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Reject payloads the CP1252 codec cannot represent."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def _config(sink: typ.IO[str], *, echo: bool = True) -> _StreamConfig:
    """Build a UTF-8 stream config for echo-guard tests."""
    return _StreamConfig(
        capture_output=True,
        echo_output=echo,
        sink=sink,
        encoding="utf-8",
        errors="replace",
    )


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _reader(chunks: cabc.Sequence[bytes]) -> asyncio.StreamReader:
    """Build a stream-reader-shaped stub for the given chunks."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def test_drain_completes_capture_when_text_sink_cannot_encode() -> None:
    """A UnicodeEncodeError from a text sink disables echo without aborting."""
    chunks = (b"Cargo metadata: ", "ś".encode(), b" ", "ń".encode())
    sink = _Cp1252TextOnlySink()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink))),
    )

    assert captured == b"".join(chunks).decode("utf-8", errors="replace"), (
        "capture must complete even when the echo sink rejects a chunk for "
        f"chunks={chunks!r}, captured={captured!r}"
    )
    assert sink.attempts == ["Cargo metadata: ", "ś"], (
        "echo must stop once a chunk is rejected: the first chunk still echoes "
        f"and the rejecting chunk counts as attempted for attempts={sink.attempts!r}"
    )


def test_drain_stops_echo_after_first_encode_failure() -> None:
    """Echo is disabled for the drain after its first UnicodeEncodeError."""
    chunks = (b"plain ", "ś".encode(), b" plain ", "ń".encode())
    sink = _Cp1252TextOnlySink()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink))),
    )

    assert captured == b"".join(chunks).decode("utf-8", errors="replace"), (
        "capture must stay complete while echo is disabled for "
        f"chunks={chunks!r}, captured={captured!r}"
    )
    assert sink.attempts == ["plain ", "ś"], (
        "later chunks must not reach the sink after the first encode failure "
        f"for attempts={sink.attempts!r}"
    )


def test_drain_warns_once_per_stream_with_structured_extras(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disable event logs exactly one structured cuprum.stream warning."""
    chunks = (b"plain ", "ś".encode(), b" plain ", "ń".encode())
    sink = _Cp1252TextOnlySink()

    with caplog.at_level(logging.WARNING, logger="cuprum.stream"):
        asyncio.run(_drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink))))

    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]
    assert len(warnings) == 1, (
        f"exactly one disable warning must be logged for records={caplog.records!r}"
    )
    record = warnings[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        "echo_disabled encoding=utf-8 error=UnicodeEncodeError"
    )
    fields = vars(record)
    assert fields["cuprum_encoding"] == "utf-8"
    assert fields["cuprum_sink_type"] == "_Cp1252TextOnlySink"
    assert fields["cuprum_error_type"] == "UnicodeEncodeError"


def test_drain_propagates_non_encoding_sink_errors() -> None:
    """Sink failures other than UnicodeEncodeError still abort the drain."""

    class _OSErrorSink:
        """Text-only sink failing with a non-encoding I/O error."""

        def write(self, _payload: str) -> int:
            """Model an unreachable sink device."""
            msg = "device unreachable"
            raise OSError(msg)

        def flush(self) -> None:
            """Model the flush call on a text stream."""

    sink = typ.cast("typ.IO[str]", _OSErrorSink())

    with pytest.raises(OSError, match="device unreachable"):
        asyncio.run(_drain(_reader((b"payload",)), _config(sink)))


def test_flush_after_disabled_echo_does_not_raise() -> None:
    """A disabled echo never re-attempts the final decoder flush write."""

    async def run_case() -> tuple[str | None, _Cp1252TextOnlySink]:
        """Reject one decoded character, then cancel holding an incomplete one."""
        reader = asyncio.StreamReader()
        reader.feed_data("ś".encode())
        reader.feed_data(b"\xc3")
        sink = _Cp1252TextOnlySink()
        task = asyncio.create_task(
            _drain(reader, _config(typ.cast("typ.IO[str]", sink), echo=True)),
        )
        await asyncio.sleep(0)
        task.cancel()
        return await task, sink

    captured, sink = asyncio.run(run_case())

    assert captured == "ś\N{REPLACEMENT CHARACTER}", (
        "capture must keep the rejected character and flush the decoder tail "
        f"after echo is disabled for captured={captured!r}"
    )
    assert sink.attempts == ["ś"], (
        "exactly one rejected write must be attempted before echo is disabled "
        f"for attempts={sink.attempts!r}"
    )
