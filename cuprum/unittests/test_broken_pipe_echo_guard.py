"""Direct coverage for the opt-in broken-pipe echo guard (#435).

The canonical drain contract lives in ``test_stream_drain.py`` and the
sibling encode guard in ``test_stream_echo_guard.py``; this module owns the
narrow broken-pipe policy added for issue #435. A presentation sink whose
destination has closed raises ``BrokenPipeError``, which by default aborts the
drain and takes the caller's ``CommandResult`` with it. Under
``BrokenPipePolicy.BEST_EFFORT`` the affected stream's echo is disabled while
capture, line observation, and child reaping continue, and the transition is
reported once per drain through the same three bounded projections the encode
guard uses. Focused relay-diagnostics cases live in
``test_relay_fallback_diagnostics.py``.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum._streams import _drain, _RelayDiagnostics, _StreamConfig
from cuprum.echo_events import (
    BrokenPipePolicy,
    EchoErrorCategory,
    EchoStream,
    RelayFallback,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_BROKEN_PIPE = "closed presentation destination"


class _BrokenPipeSink:
    """Text-only sink whose destination has closed under it.

    ``mode`` names the call that fails, so the same double covers a sink that
    breaks on the text write and one that accepts the write but cannot flush —
    the two halves of one ``try`` in ``_echo_write``.
    """

    def __init__(self, *, mode: typ.Literal["write", "flush"] = "write") -> None:
        """Record the failing call and the payloads offered before it."""
        self._mode = mode
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the attempt, then fail the way a closed reader does."""
        self.attempts.append(payload)
        if self._mode == "write":
            raise BrokenPipeError(_BROKEN_PIPE)
        return len(payload)

    def flush(self) -> None:
        """Fail on flush when this double models a flush-side break."""
        if self._mode == "flush":
            raise BrokenPipeError(_BROKEN_PIPE)


class _BrokenBinaryBuffer:
    """Binary buffer whose destination has closed under it.

    ``raw`` holds the bytes the buffer *accepted*, not the bytes it was
    offered: a write that raises a broken pipe delivered nothing downstream,
    so recording it would overstate what the sink saw.
    """

    def __init__(self, *, mode: typ.Literal["write", "flush"] = "write") -> None:
        """Record the failing call and the raw payloads that landed."""
        self._mode = mode
        self.raw: list[bytes] = []

    def write(self, payload: bytes) -> int:
        """Accept raw bytes, or fail the way a closed reader does."""
        if self._mode == "write":
            raise BrokenPipeError(_BROKEN_PIPE)
        self.raw.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Fail on flush when this double models a flush-side break."""
        if self._mode == "flush":
            raise BrokenPipeError(_BROKEN_PIPE)


class _BrokenBinarySink:
    """Sink exposing a broken ``buffer``, modelling the binary fast path."""

    def __init__(self, *, mode: typ.Literal["write", "flush"] = "write") -> None:
        """Track any text write; the drain must never take that path."""
        self.text_writes: list[str] = []
        self.buffer = _BrokenBinaryBuffer(mode=mode)

    def write(self, payload: str) -> int:
        """Record any text write attempt."""
        self.text_writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


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


def _config(
    sink: typ.IO[str],
    *,
    echo: bool = True,
    policy: BrokenPipePolicy = BrokenPipePolicy.BEST_EFFORT,
) -> _StreamConfig:
    """Build a UTF-8 stream config for broken-pipe tests."""
    return _StreamConfig(
        capture_output=True,
        echo_output=echo,
        sink=sink,
        encoding="utf-8",
        errors="replace",
        broken_pipe_policy=policy,
    )


_EXPECTED_BROKEN_PIPE_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.BROKEN_PIPE,
)


def test_best_effort_completes_capture_when_sink_pipe_is_broken() -> None:
    """A BrokenPipeError from a text sink disables echo without aborting."""
    chunks = (b"hello ", b"world")
    sink = _BrokenPipeSink()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink))),
    )

    assert captured == b"".join(chunks).decode("utf-8"), (
        "capture must complete even when the echo sink's pipe is broken for "
        f"chunks={chunks!r}, captured={captured!r}"
    )
    assert sink.attempts == ["hello "], (
        "the broken write must be the last echo attempt for the drain, got "
        f"attempts={sink.attempts!r}"
    )


def test_best_effort_stops_echo_after_the_first_broken_pipe() -> None:
    """Echo is disabled for the drain after its first BrokenPipeError."""
    chunks = (b"one ", b"two ", b"three")
    sink = _BrokenPipeSink()

    asyncio.run(_drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink))))

    assert sink.attempts == ["one "], (
        "no later chunk may reach the sink once the pipe has broken for "
        f"attempts={sink.attempts!r}"
    )


@pytest.mark.parametrize(
    ("mode", "sink_factory"),
    [
        pytest.param("write", _BrokenPipeSink, id="text-write"),
        pytest.param("flush", _BrokenPipeSink, id="text-flush"),
    ],
)
def test_best_effort_recovers_from_text_write_and_flush_failures(
    mode: typ.Literal["write", "flush"],
    sink_factory: cabc.Callable[..., typ.IO[str]],
) -> None:
    """Both halves of ``_echo_write``'s single try are covered for text sinks."""
    sink = sink_factory(mode=mode)
    chunks = (b"payload",)

    captured = asyncio.run(_drain(_reader(chunks), _config(sink)))

    assert captured == "payload", (
        "capture must complete whether the text write or its flush broke for "
        f"mode={mode!r}, captured={captured!r}"
    )


@pytest.mark.parametrize(
    ("mode", "buffer"),
    [
        pytest.param("write", b"", id="binary-buffer-write"),
        pytest.param("flush", b"payload", id="binary-buffer-flush"),
    ],
)
def test_best_effort_recovers_from_binary_buffer_failures(
    mode: typ.Literal["write", "flush"],
    buffer: bytes,
) -> None:
    """The binary ``.buffer`` fast path is covered on both calls, byte-for-byte."""
    sink = _BrokenBinarySink(mode=mode)
    chunks = (b"payload",)

    captured = asyncio.run(
        _drain(_reader(chunks), _config(typ.cast("typ.IO[str]", sink)))
    )

    assert captured == "payload", (
        f"capture must complete for mode={mode!r}, captured={captured!r}"
    )
    assert b"".join(sink.buffer.raw) == buffer, (
        "the binary path must forward the original bytes and stop at the point "
        f"it broke for mode={mode!r}, raw={sink.buffer.raw!r}"
    )
    assert sink.text_writes == [], "the binary fast path must not fall back to text"


def test_best_effort_warns_once_with_structured_extras(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The disable event logs exactly one structured cuprum.stream warning."""
    sink = _BrokenPipeSink()
    chunks = (b"one ", b"two ", b"three")
    relay_diagnostics = _RelayDiagnostics()

    with caplog.at_level(logging.WARNING, logger="cuprum.stream"):
        asyncio.run(
            _drain(
                _reader(chunks),
                _config(typ.cast("typ.IO[str]", sink)),
                relay_diagnostics=relay_diagnostics,
            ),
        )
    relay_diagnostics.settle()

    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]
    assert len(warnings) == 1, (
        f"exactly one disable warning must be logged for records={caplog.records!r}"
    )
    record = warnings[0]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "echo_disabled_stream_rejected_output"
    assert record.exc_info is None, (
        "the handled sink failure must not carry the original exception: "
        f"exc_info={record.exc_info!r}"
    )
    fields = vars(record)
    assert fields["cuprum_operation"] == "echo_chunk"
    assert fields["cuprum_stream"] == "stdout"
    assert fields["cuprum_transition"] == "echo_disabled"
    assert fields["cuprum_error_category"] == "broken_pipe"
    assert "cuprum_encoding" not in fields, (
        "the sink encoding must not reach the warning record"
    )
    assert "cuprum_sink_type" not in fields, (
        "the sink type must not reach the warning record"
    )
    assert _BROKEN_PIPE not in record.getMessage(), (
        "the exception text must not reach the log"
    )
    assert relay_diagnostics.snapshot() == (_EXPECTED_BROKEN_PIPE_FALLBACK,), (
        "exactly one result record must describe the handled transition, got "
        f"{relay_diagnostics.snapshot()!r}"
    )


def test_strict_policy_propagates_the_broken_pipe() -> None:
    """Under the default policy a broken pipe still aborts the drain."""

    class _StrictBrokenSink:
        """Text-only sink whose destination has closed under it."""

        def write(self, _payload: str) -> int:
            """Model a closed presentation destination."""
            raise BrokenPipeError(_BROKEN_PIPE)

        def flush(self) -> None:
            """Model the flush call on a text stream."""

    sink = typ.cast("typ.IO[str]", _StrictBrokenSink())

    with pytest.raises(BrokenPipeError, match=_BROKEN_PIPE):
        asyncio.run(
            _drain(
                _reader((b"payload",)), _config(sink, policy=BrokenPipePolicy.STRICT)
            ),
        )


def test_strict_is_the_default_policy() -> None:
    """A config that names no policy runs the strict path."""
    assert (
        _StreamConfig(
            capture_output=True,
            echo_output=True,
            sink=typ.cast("typ.IO[str]", _BrokenPipeSink()),
            encoding="utf-8",
            errors="replace",
        ).broken_pipe_policy
        is BrokenPipePolicy.STRICT
    ), "the default must stay strict, so existing callers are unaffected"


def test_best_effort_propagates_non_broken_pipe_os_errors() -> None:
    """A sink error that is not a broken pipe still aborts the drain."""

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


def test_flush_after_broken_pipe_does_not_reattempt_the_write() -> None:
    """A disabled echo never re-attempts the final decoder flush write."""

    async def run_case() -> tuple[str | None, _BrokenPipeSink]:
        """Break the pipe on the payload, then cancel holding a partial char."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"hello ")
        reader.feed_data(b"\xc3")
        sink = _BrokenPipeSink()
        task = asyncio.create_task(
            _drain(reader, _config(typ.cast("typ.IO[str]", sink))),
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        return await task, sink

    captured, sink = asyncio.run(run_case())

    assert captured == "hello \N{REPLACEMENT CHARACTER}", (
        "capture must flush the decoder tail after echo is disabled for "
        f"captured={captured!r}"
    )
    assert sink.attempts == ["hello "], (
        "exactly one broken write must be attempted before echo is disabled "
        f"for attempts={sink.attempts!r}"
    )


def test_broken_pipe_on_the_final_decoder_flush_is_recovered() -> None:
    """A pipe that breaks first on the end-of-stream flush is still handled."""
    sink = _BrokenPipeSink()

    async def run_case() -> str | None:
        """Hold a split multibyte character back so the flush carries text."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xc3")
        reader.feed_eof()
        return await _drain(reader, _config(typ.cast("typ.IO[str]", sink)))

    captured = asyncio.run(run_case())

    assert captured == "\N{REPLACEMENT CHARACTER}", (
        f"capture must survive a flush-side break, got captured={captured!r}"
    )
    assert sink.attempts == ["\N{REPLACEMENT CHARACTER}"], (
        "the flush must deliver the decoder tail to the sink exactly once, got "
        f"attempts={sink.attempts!r}"
    )
