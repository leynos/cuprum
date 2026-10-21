"""Test doubles and models for direct subprocess stream-drain tests.

This module groups queued-reader doubles, ``_StreamConfig`` builders, Hypothesis
payload and chunk strategies, and expected line-emission models. The helpers let
tests exercise ``cuprum._streams`` directly without invoking command or
pipeline runners. It also names the complete drain result used by consumer
doubles, so payloads cannot silently stand in for capture records.
"""

from __future__ import annotations

import asyncio
import codecs
import dataclasses
import io
import typing as typ

from hypothesis import strategies as st

from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._streams import _StreamConfig
from cuprum._subprocess_wait_types import _StreamConsumerTask
from cuprum.capture_policy import CapturePolicy, FullCapture

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# One drain's captured payload: text in the ordinary mode, the child's bytes
# untouched in the byte-exact one.
type CapturedPayload = str | bytes
# What a test helper returns when it reports a capture and nothing else.
type CapturedOrNone = CapturedPayload | None
# One stream's payload paired with another value the helper also reports.
type CapturedPair[T] = tuple[CapturedOrNone, T]
# The whole value returned by a settled drain, including its capture record.
type CapturedDrain = _CapturedDrain
# A settled drain paired with another value the helper also reports.
type CapturedDrainPair[T] = tuple[CapturedDrain, T]
# A reader the drain owns, named for test doubles that follow its task shape.
type ConsumerTask = _StreamConsumerTask

_FULL_CAPTURE = FullCapture()
_BOTH_CAPTURED: tuple[FullCapture, FullCapture] = (FullCapture(), FullCapture())
_NONE_CAPTURED: tuple[None, None] = (None, None)

__all__ = [
    "_BOTH_CAPTURED",
    "_NONE_CAPTURED",
    "CapturedDrain",
    "CapturedDrainPair",
    "CapturedOrNone",
    "CapturedPair",
    "CapturedPayload",
    "ConsumerTask",
    "captured",
]

_LINE_BOUNDARY_CHARACTERS = (
    "\n",
    "\r",
    "\v",
    "\f",
    "\x1c",
    "\x1d",
    "\x1e",
    "\x85",
    "\u2028",
    "\u2029",
)
_PROPERTY_MAX_EXAMPLES = 24


class _ChunkedReader:
    """Stub stream reader yielding queued chunks before EOF."""

    def __init__(self, chunks: cabc.Sequence[bytes]) -> None:
        """Store chunks for sequential ``read`` calls."""
        self._chunks = list(chunks)
        self.read_sizes: list[int] = []

    async def read(self, size: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        self.read_sizes.append(size)
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
    capture: CapturePolicy | None = _FULL_CAPTURE,
    echo: bool = False,
    errors: str = "replace",
) -> _StreamConfig:
    """Build a UTF-8 stream config for direct drain tests."""
    return _StreamConfig(
        capture=capture,
        echo_output=echo,
        sink=sink,
        encoding="utf-8",
        errors=errors,
    )


def _bounded_config(max_line_chars: int | None) -> _StreamConfig:
    """Build a capturing config with an explicit line-retention bound."""
    return _StreamConfig(
        capture=FullCapture(),
        echo_output=False,
        sink=io.StringIO(),
        encoding="utf-8",
        errors="replace",
        max_line_chars=max_line_chars,
    )


def _byte_config(
    sink: typ.IO[str],
    *,
    capture: CapturePolicy | None = _FULL_CAPTURE,
    echo: bool = False,
    errors: str = "replace",
) -> _StreamConfig:
    """Build a byte-exact UTF-8 stream config for direct drain tests."""
    return dataclasses.replace(
        _config(sink, capture=capture, echo=echo, errors=errors),
        capture_bytes=True,
    )


def captured(payload: CapturedPayload | None) -> _CapturedDrain:
    """Report a consumer's payload without inventing a capture record."""
    return _CapturedDrain(payload=payload)


def _split_at(payload: bytes, cut_points: cabc.Sequence[int]) -> tuple[bytes, ...]:
    """Split a payload at sorted, deduplicated cut points."""
    bounds = sorted({point for point in cut_points if 0 < point < len(payload)})
    pieces: list[bytes] = []
    start = 0
    for bound in bounds:
        pieces.append(payload[start:bound])
        start = bound
    pieces.append(payload[start:])
    return tuple(piece for piece in pieces if piece)


def _count_text_writes(chunks: tuple[bytes, ...]) -> int:
    """Count the non-empty text writes the drain's echo decoder will make."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    writes = 0
    for chunk in chunks:
        if decoder.decode(chunk):
            writes += 1
    if decoder.decode(b"", final=True):
        writes += 1
    return writes


@st.composite
def _payload_and_chunks(draw: st.DrawFn) -> tuple[bytes, tuple[bytes, ...]]:
    """Generate payload bytes and arbitrary stream chunks."""
    payload = draw(st.binary(min_size=0, max_size=512))
    if len(payload) <= 1:
        return payload, (payload,) if payload else ()

    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=len(payload) - 1),
            min_size=0,
            max_size=min(8, len(payload) - 1),
            unique=True,
        ),
    )
    return payload, _split_at(payload, cut_points)


@st.composite
def _line_text_and_chunks(draw: st.DrawFn) -> tuple[str, tuple[bytes, ...]]:
    """Generate every supported line ending over arbitrary byte partitions."""
    fragments = draw(
        st.lists(
            st.text(
                alphabet=st.characters(
                    blacklist_characters=_LINE_BOUNDARY_CHARACTERS,
                ),
                max_size=12,
            ),
            min_size=len(_LINE_BOUNDARY_CHARACTERS) + 1,
            max_size=len(_LINE_BOUNDARY_CHARACTERS) + 1,
        )
    )
    segments, tail = fragments[:-1], fragments[-1]
    text = (
        "".join(
            f"{fragment}{boundary}"
            for fragment, boundary in zip(
                segments,
                _LINE_BOUNDARY_CHARACTERS,
                strict=True,
            )
        )
        + tail
    )
    payload = text.encode("utf-8")
    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=max(1, len(payload) - 1)),
            max_size=min(16, max(0, len(payload) - 1)),
            unique=True,
        )
    )
    return text, _split_at(payload, cut_points)


def _decode_chunks(chunks: cabc.Sequence[bytes]) -> str:
    """Decode chunks as one stream for comparison with text-sink echoing."""
    return b"".join(chunks).decode("utf-8", errors="replace")


def _expected_emitted_lines(payload: bytes) -> list[str]:
    """Model ``str.splitlines()`` stream-line emission independently."""
    lines = payload.decode("utf-8", errors="replace").splitlines(keepends=True)
    if not lines:
        return []

    remainder = ""
    if not lines[-1].endswith(_LINE_BOUNDARY_CHARACTERS):
        remainder = lines.pop()

    def strip_ending(line: str) -> str:
        """Strip one trailing ``str.splitlines()`` boundary."""
        if line.endswith("\r\n"):
            return line[:-2]
        if line.endswith(_LINE_BOUNDARY_CHARACTERS):
            return line[:-1]
        return line

    emitted = [strip_ending(line) for line in lines]
    if remainder:
        emitted.append(strip_ending(remainder))
    return emitted
