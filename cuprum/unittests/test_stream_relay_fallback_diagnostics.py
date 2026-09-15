"""Low-level relay-fallback diagnostics coverage for stream drains (#356)."""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum._streams import _drain, _RelayDiagnostics, _StreamConfig
from cuprum.echo_events import EchoErrorCategory, EchoStream, RelayFallback

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _Cp1252TextOnlySink:
    """Text-only sink rejecting payloads CP1252 cannot represent."""

    def __init__(self) -> None:
        """Record each attempted write payload."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
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


class _RecordingBinaryBuffer:
    """Binary buffer capturing the raw bytes a drain hands to the sink."""

    def __init__(self) -> None:
        """Start with no captured bytes."""
        self.raw: list[bytes] = []

    def write(self, payload: bytes) -> int:
        """Capture raw bytes; CP1252 is deliberately never applied."""
        self.raw.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a buffered writer."""


class _BinaryBufferSink:
    """Sink exposing a writable ``buffer``, modelling the binary fast path."""

    def __init__(self) -> None:
        """Track any text write; the drain must never take that path."""
        self.text_writes: list[str] = []
        self.buffer = _RecordingBinaryBuffer()

    def write(self, payload: str) -> int:
        """Record any text write attempt."""
        self.text_writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _NullScope:
    """Context manager standing in for 'no observer registered'."""

    def __enter__(self) -> None:
        """Enter the no-op scope."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object,
    ) -> None:
        """Exit the no-op scope."""


def _null_scope() -> _NullScope:
    """Return the no-op scope."""
    return _NullScope()


def _config_with_stream(
    sink: typ.IO[str],
    *,
    capture: bool = True,
    echo: bool = True,
    stream: EchoStream = EchoStream.STDOUT,
) -> _StreamConfig:
    """Build a UTF-8 config pinned to one named output stream."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=echo,
        sink=sink,
        encoding="utf-8",
        errors="replace",
        stream=stream,
    )


_EXPECTED_STDOUT_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)


def test_binary_buffer_sink_receives_original_bytes() -> None:
    """A sink with a writable binary buffer gets raw bytes, no diagnostics."""
    sink = _BinaryBufferSink()
    chunks = ("safé ".encode(), "wörld ś".encode())
    relay_diagnostics = _RelayDiagnostics()

    captured = asyncio.run(
        _drain(
            _reader(chunks),
            _config_with_stream(
                typ.cast("typ.IO[str]", sink), capture=False, echo=True
            ),
            relay_diagnostics=relay_diagnostics,
        ),
    )
    relay_diagnostics.settle()

    assert captured is None
    assert b"".join(sink.buffer.raw) == b"".join(chunks), (
        "the binary fast path must forward the original child bytes unchanged"
    )
    assert sink.text_writes == [], "no text write may be attempted"
    assert relay_diagnostics.snapshot() == ()


def test_text_only_failure_records_once_across_all_surfaces(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One disablement yields one warning with categorical extras only."""
    sink = _Cp1252TextOnlySink()
    chunks = (b"plain ", "ś".encode(), b" tail")
    relay_diagnostics = _RelayDiagnostics()

    with caplog.at_level(logging.WARNING, logger="cuprum.stream"):
        captured = asyncio.run(
            _drain(
                _reader(chunks),
                _config_with_stream(typ.cast("typ.IO[str]", sink)),
                relay_diagnostics=relay_diagnostics,
            ),
        )
    relay_diagnostics.settle()

    assert captured == b"".join(chunks).decode("utf-8", errors="replace"), (
        "capture must complete even though the sink rejected the output"
    )
    assert sink.attempts == ["plain ", "ś"], (
        "the rejecting write must be the last echo attempt for the drain"
    )
    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]
    assert len(warnings) == 1, f"exactly one warning expected, got {caplog.records!r}"
    record = warnings[0]
    assert record.getMessage() == "echo_disabled_stream_rejected_output"
    assert record.exc_info is None, (
        "the original exception object must not ride on the record"
    )
    assert record.args in {None, ()}, (
        f"positional args must stay empty, got {record.args!r}"
    )
    fields = vars(record)
    assert fields["cuprum_operation"] == "echo_chunk"
    assert fields["cuprum_stream"] == "stdout"
    assert fields["cuprum_transition"] == "echo_disabled"
    assert fields["cuprum_error_category"] == "unicode_encode"
    rendered = record.getMessage()
    assert "ś" not in rendered, "the rejected payload must not reach the log"
    assert "cp1252" not in rendered, "the sink encoding must not reach the log"
    assert "cuprum_encoding" not in fields
    assert "cuprum_sink_type" not in fields
    assert "cuprum_error_type" not in fields
    assert relay_diagnostics.snapshot() == (_EXPECTED_STDOUT_FALLBACK,)


def test_drain_collects_diagnostics_without_observers_or_capture() -> None:
    """Diagnostics are collected with no observer registered, no capture."""
    sink = _Cp1252TextOnlySink()
    relay_diagnostics = _RelayDiagnostics()

    with _null_scope():
        captured = asyncio.run(
            _drain(
                _reader(("ś".encode(), b" tail")),
                _config_with_stream(
                    typ.cast("typ.IO[str]", sink), capture=False, echo=True
                ),
                relay_diagnostics=relay_diagnostics,
            ),
        )
    relay_diagnostics.settle()

    assert captured is None, "capture-disabled drains still return None"
    assert sink.attempts == ["ś"], (
        "the disablement must be the first and only echo attempt"
    )
    assert relay_diagnostics.snapshot() == (_EXPECTED_STDOUT_FALLBACK,)


def test_non_encoding_sink_error_still_propagates() -> None:
    """Sink failures other than UnicodeEncodeError are not relay fallbacks."""

    class _OSErrorSink:
        """Text-only sink failing with a non-encoding I/O error."""

        def write(self, _payload: str) -> int:
            """Model an unreachable sink device."""
            msg = "device unreachable"
            raise OSError(msg)

        def flush(self) -> None:
            """Model the flush call on a text stream."""

    with pytest.raises(OSError, match="device unreachable"):
        asyncio.run(
            _drain(
                _reader((b"payload",)),
                _config_with_stream(typ.cast("typ.IO[str]", _OSErrorSink())),
            ),
        )
