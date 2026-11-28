"""The drain's end-of-stream hook and its byte-tap composition.

A live byte reader must learn that its stream ended exactly once, and only for
a genuine end. These tests pin the four conditions that separate a real end
from a cancellation or a failure, and the order in which one chunk reaches
capture, echo, the byte tap, and the line feeder.

The tap is part of the stream configuration rather than a drain argument, so a
byte run and a text run call the same entry point; these tests therefore drive
``_consume_stream`` directly through the same config the production spawn
builds.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import io
import typing as typ

import pytest

from cuprum._stream_tap import _StreamTap
from cuprum._streams import _consume_stream
from cuprum.unittests._stream_drain_support import _config, _reader

if typ.TYPE_CHECKING:
    from cuprum._stream_config import _StreamConfig


class _RecordingTap:
    """A byte tap that records each chunk and each end-of-stream call."""

    def __init__(self) -> None:
        """Start with no observations."""
        self.chunks: list[bytes] = []
        self.eof_calls = 0

    async def on_chunk(self, chunk: bytes) -> None:
        """Record one raw chunk."""
        self.chunks.append(chunk)

    def on_eof(self) -> None:
        """Record one end-of-stream observation."""
        self.eof_calls += 1

    def tap(self) -> _StreamTap:
        """Return the immutable tap the config carries."""
        return _StreamTap(on_chunk=self.on_chunk, on_eof=self.on_eof)


def _tapped(config: _StreamConfig, tap: _RecordingTap) -> _StreamConfig:
    """Return *config* with *tap* attached, rebuilding its accumulator."""
    return dc.replace(config, tap=tap.tap())


def test_eof_hook_fires_once_after_every_chunk() -> None:
    """A genuine end calls the hook once, after the last tap observation."""

    async def scenario() -> None:
        """Drain two chunks to EOF through a recording tap."""
        tap = _RecordingTap()
        config = _tapped(_config(io.StringIO()), tap)
        drained = await _consume_stream(_reader([b"one", b"two"]), config)
        assert tap.chunks == [b"one", b"two"], (
            f"the tap must see every raw chunk in order, got {tap.chunks!r}"
        )
        assert tap.eof_calls == 1, (
            f"a genuine end must call the hook exactly once, got {tap.eof_calls}"
        )
        assert drained.payload == "onetwo", (
            f"capture must still hold the whole stream, got {drained.payload!r}"
        )

    asyncio.run(scenario())


def test_eof_hook_is_not_called_when_the_read_is_cancelled() -> None:
    """Cancelling a waiting read is not an end of stream."""

    async def scenario() -> None:
        """Cancel a drain whose reader never produces a chunk."""
        tap = _RecordingTap()
        config = _tapped(_config(io.StringIO()), tap)
        reader = asyncio.StreamReader()
        drain = asyncio.create_task(_consume_stream(reader, config))
        await asyncio.sleep(0)
        drain.cancel()
        # A cancelled read is absorbed by the drain: the partial capture is
        # finalized and the task returns rather than propagating cancellation.
        drained = await drain
        assert not drained.payload, (
            f"a cancelled read captures the empty prefix, got {drained.payload!r}"
        )
        assert tap.eof_calls == 0, (
            f"cancellation must not claim EOF, got {tap.eof_calls} calls"
        )

    asyncio.run(scenario())


def test_eof_hook_is_not_called_when_the_tap_fails() -> None:
    """A failing byte tap stops the drain without publishing an end."""

    async def scenario() -> None:
        """Raise from the tap while a chunk is delivered."""
        eof_calls = 0

        def failing_chunk(_chunk: bytes) -> None:
            """Fail the chunk delivery."""
            message = "tap failed"
            raise RuntimeError(message)

        def record_eof() -> None:
            """Count end-of-stream observations."""
            nonlocal eof_calls
            eof_calls += 1

        config = dc.replace(
            _config(io.StringIO()),
            tap=_StreamTap(on_chunk=failing_chunk, on_eof=record_eof),
        )
        with pytest.raises(RuntimeError, match="tap failed"):
            await _consume_stream(_reader([b"payload"]), config)
        assert eof_calls == 0, "a failed delivery must not publish EOF"

    asyncio.run(scenario())


def test_byte_tap_runs_before_line_decoding_over_split_utf8() -> None:
    """Each raw chunk reaches the tap before the line feeder decodes it.

    The order is recorded in one list rather than inferred from what each
    consumer received: a change that ran the line feeder first would still
    deliver both observations and would pass a content-only check.
    """

    async def scenario() -> None:
        """Feed a split euro sign and record both consumers in one order."""

        class _OrderingTap(_RecordingTap):
            """A tap that records its observations in the shared order list."""

            def __init__(self, order: list[tuple[str, bytes | str]]) -> None:
                """Start empty, appending to *order*."""
                super().__init__()
                self._order = order

            async def on_chunk(self, chunk: bytes) -> None:
                """Record the tap observation before recording the chunk."""
                self._order.append(("tap", chunk))
                await super().on_chunk(chunk)

        order: list[tuple[str, bytes | str]] = []
        tap = _OrderingTap(order)
        config = _tapped(_config(io.StringIO()), tap)

        def observe(line: str) -> None:
            """Record each decoded line in the shared order list."""
            order.append(("line", line))

        await _consume_stream(
            _reader([b"\xe2", b"\x82\xac\n"]), config, on_line=observe
        )
        assert tap.chunks == [b"\xe2", b"\x82\xac\n"], (
            f"the tap must see the raw split sequence, got {tap.chunks!r}"
        )
        assert order == [
            ("tap", b"\xe2"),
            ("tap", b"\x82\xac\n"),
            ("line", "€"),
        ], f"the tap must precede line decoding, got {order!r}"

    asyncio.run(scenario())


def test_echo_write_precedes_the_byte_tap() -> None:
    """One chunk is echoed before the live tap observes it."""

    async def scenario() -> None:
        """Record the sink write and the tap observation in one list."""
        order: list[str] = []

        class _Sink(io.StringIO):
            """A text sink that records each write in the shared order list."""

            def write(self, text: str) -> int:
                """Record the echo write before delegating."""
                order.append(f"sink:{text.strip()}")
                return super().write(text)

        def on_chunk(chunk: bytes) -> None:
            """Record the tap observation."""
            order.append(f"tap:{chunk.decode().strip()}")

        config = dc.replace(
            _config(_Sink(), echo=True),
            tap=_StreamTap(on_chunk=on_chunk, on_eof=lambda: order.append("eof")),
        )
        await _consume_stream(_reader([b"hello\n"]), config)
        assert order == ["sink:hello", "tap:hello", "eof"], (
            f"echo must precede the tap and EOF must follow both, got {order!r}"
        )

    asyncio.run(scenario())


def test_absent_stream_calls_no_hook_and_reports_the_mode() -> None:
    """A stream that was never attached is not an end of stream."""

    async def scenario() -> None:
        """Drain an absent byte-mode stream with a tap attached."""
        tap = _RecordingTap()
        byte_config = dc.replace(
            _config(io.StringIO()),
            capture_bytes=True,
        )
        config = _tapped(byte_config, tap)
        drained = await _consume_stream(None, config)
        assert tap.eof_calls == 0, "an absent stream must not publish EOF"
        assert drained.payload == b"", (
            f'an absent byte-mode stream reports b"", got {drained.payload!r}'
        )

    asyncio.run(scenario())


def test_tap_observes_every_chunk_under_cancellation_free_backpressure() -> None:
    """A slow tap applies backpressure without reordering or losing a chunk."""

    async def scenario() -> None:
        """Delay each tap call and assert the received order."""
        received: list[bytes] = []

        async def slow_tap(chunk: bytes) -> None:
            """Yield to the loop, then record the chunk."""
            await asyncio.sleep(0)
            received.append(chunk)

        config = dc.replace(
            _config(io.StringIO()),
            tap=_StreamTap(on_chunk=slow_tap, on_eof=lambda: None),
        )
        chunks = [f"chunk-{index}".encode() for index in range(20)]
        await _consume_stream(_reader(chunks), config)
        assert received == chunks, "a slow tap must receive every chunk in order"

    asyncio.run(scenario())
