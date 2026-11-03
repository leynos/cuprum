"""Regression tests for bounded echoing through the stream drain."""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest

from cuprum._streams import _drain, _StreamConfig
from cuprum.capture_policy import CapturePolicy, FullCapture
from cuprum.echo_events import EchoErrorCategory
from cuprum.echo_observation import observe_echo

_BOUND = 50


class _ChunkedReader:
    """Reader stub yielding queued chunks before EOF."""

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        """Store chunks for sequential reads without needing an event loop."""
        self._chunks = list(chunks)

    async def read(self, _size: int) -> bytes:
        """Return the next chunk, or EOF when all chunks were consumed."""
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def _reader(chunks: tuple[bytes, ...]) -> asyncio.StreamReader:
    """Build a reader containing *chunks* and then EOF."""
    return typ.cast("asyncio.StreamReader", _ChunkedReader(chunks))


def _config(
    sink: typ.IO[str],
    *,
    capture: CapturePolicy | None,
    errors: str = "replace",
) -> _StreamConfig:
    """Build a bounded UTF-8 echo configuration for one drain regression."""
    return _StreamConfig(
        capture=capture,
        echo_output=True,
        echo_max_line_bytes=_BOUND,
        sink=sink,
        encoding="utf-8",
        errors=errors,
    )


def test_echo_only_unterminated_line_keeps_a_bounded_mirror() -> None:
    """A capture-disabled drain retains no oversized unfinished echoed line."""
    payload = b"z" * (2 * 1024 * 1024)
    chunks = tuple(
        payload[start : start + 8192] for start in range(0, len(payload), 8192)
    )
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=None)),
    )

    assert captured.payload is None, "echo-only drains must not create captured output"
    assert len(sink.getvalue().encode()) <= _BOUND, (
        "the unfinished echoed line must remain bounded without capture"
    )


def test_bounded_echo_rejects_utf16_before_draining() -> None:
    """Bounded raw-line echo rejects UTF-16 before it can corrupt a sink."""
    config = _StreamConfig(
        capture=FullCapture(),
        echo_output=True,
        echo_max_line_bytes=_BOUND,
        sink=io.StringIO(),
        encoding="utf-16",
        errors="strict",
    )

    with pytest.raises(ValueError, match="ASCII-compatible stateless encoding"):
        asyncio.run(_drain(_reader(("payload".encode("utf-16"),)), config))


def test_unbounded_utf16_echo_remains_supported() -> None:
    """Unbounded echo retains the existing UTF-16 streaming behaviour."""
    payload = "payload".encode("utf-16")
    sink = io.StringIO()
    config = _StreamConfig(
        capture=FullCapture(),
        echo_output=True,
        echo_max_line_bytes=None,
        sink=sink,
        encoding="utf-16",
        errors="strict",
    )

    captured = asyncio.run(_drain(_reader((payload,)), config))

    assert captured.payload == "payload", (
        "unbounded capture must decode the full payload"
    )
    assert sink.getvalue() == "payload", "unbounded echo must still mirror UTF-16"


class _Cp1252TextOnlySink:
    """Text-only sink recording writes and rejecting unencodable text."""

    def __init__(self) -> None:
        """Record each attempted write payload in order."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


_REPEATED_COUNT = 3


def _chunked(payload: bytes, size: int) -> tuple[bytes, ...]:
    """Split *payload* into fixed-size chunks, including a short final chunk."""
    return tuple(
        payload[start : start + size] for start in range(0, len(payload), size)
    )


@pytest.mark.parametrize(
    ("body", "ending", "chunk_size", "expected_line"),
    [
        (
            b"a" * 200,
            b"\r\n",
            3,
            "a" * 23 + "… [truncated 177 bytes]\r\n",
        ),
        (
            ("☃" * 40).encode(),
            b"\n",
            7,
            "☃" * 8 + "… [truncated 96 bytes]\n",
        ),
    ],
    ids=["ascii-crlf-split", "utf8-multibyte-split"],
)
def test_repeated_oversized_lines_are_each_bounded_and_captured(
    body: bytes,
    ending: bytes,
    chunk_size: int,
    expected_line: str,
) -> None:
    """Each repeated over-limit line is bounded identically and captured whole."""
    payload = (body + ending) * _REPEATED_COUNT
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(
            _reader(_chunked(payload, chunk_size)),
            _config(sink, capture=FullCapture()),
        )
    )

    assert captured.payload == payload.decode("utf-8", "replace"), (
        "capture must retain every source byte rather than the bounded mirror"
    )
    assert sink.getvalue() == expected_line * _REPEATED_COUNT, (
        f"unexpected bounded echo, got={sink.getvalue()!r}"
    )
    echoed_lines = sink.getvalue().splitlines(keepends=True)
    assert len(echoed_lines) == _REPEATED_COUNT, (
        f"expected {_REPEATED_COUNT} echoed lines, got={echoed_lines!r}"
    )
    assert all(len(line.encode()) <= _BOUND for line in echoed_lines), (
        "every echoed line must respect the configured byte bound"
    )


def test_rejected_bounded_echo_does_not_observe_truncation() -> None:
    """A sink-rejected bounded write emits only its encoding-failure event."""
    sink = _Cp1252TextOnlySink()
    events = []
    payload = "ś".encode() + b"x" * 80 + b"\n"

    with observe_echo(events.append):
        captured = asyncio.run(
            _drain(
                _reader((payload,)),
                _config(
                    typ.cast("typ.IO[str]", sink),
                    capture=FullCapture(),
                    errors="strict",
                ),
            )
        )

    assert captured.payload == payload.decode(), "capture must retain the rejected line"
    assert [event.error_category for event in events] == [
        EchoErrorCategory.UNICODE_ENCODE
    ], f"only the failed write must be observed, got={events!r}"
    assert len(sink.attempts) == 1, (
        "disabled echo must not retry later writes or the final decoder flush"
    )
