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
import codecs
import logging
import typing as typ

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cuprum._streams import _drain, _RelayDiagnostics, _StreamConfig
from cuprum.echo_events import EchoErrorCategory, EchoStream, RelayFallback

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_MAX_EXAMPLES = 24
_CP1252_REJECTED_CHARACTERS = "śńąęółżźć"
_CP1252_SAFE_CHARACTERS = "Cargo metadata plain text 0123456789 \n"


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
    assert record.getMessage() == "echo_disabled_stream_rejected_output"
    assert record.exc_info is None, (
        "the handled sink failure must not carry the original exception: "
        f"exc_info={record.exc_info!r}"
    )
    fields = vars(record)
    assert fields["cuprum_operation"] == "echo_chunk"
    assert fields["cuprum_stream"] == "stdout"
    assert fields["cuprum_transition"] == "echo_disabled"
    assert fields["cuprum_error_category"] == "unicode_encode"
    assert "cuprum_encoding" not in fields, (
        "the sink encoding must not reach the warning record"
    )
    assert "cuprum_sink_type" not in fields, (
        "the sink type must not reach the warning record"
    )


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


@st.composite
def _echo_guard_case(draw: st.DrawFn) -> tuple[bytes, tuple[bytes, ...], int]:
    """Generate UTF-8 payload, chunk partition, and first failing write.

    The payload mixes CP1252-safe text with at least one rejected character.
    Cut points fall on arbitrary byte offsets, so a partition may split a
    multibyte UTF-8 sequence across chunks exactly as a real pipe read would.
    The failing write index is a 1-based sink-write position bounded by the
    exact number of text writes the drain will make, so the failure always
    lands on a real chunk write while echo is enabled and never on the empty
    final flush.

    Returns
    -------
    tuple[bytes, tuple[bytes, ...], int]
        The complete UTF-8 payload, its byte-chunk partition, and the 1-based
        sink write that must raise ``UnicodeEncodeError``.
    """
    safe = draw(
        st.text(alphabet=_CP1252_SAFE_CHARACTERS, min_size=1, max_size=48),
    )
    rejected = draw(st.sampled_from(_CP1252_REJECTED_CHARACTERS))
    tail = draw(
        st.text(alphabet=_CP1252_SAFE_CHARACTERS, min_size=0, max_size=24),
    )
    payload = (safe + rejected + tail).encode()

    cut_points = draw(
        st.lists(
            st.integers(min_value=1, max_value=len(payload) - 1),
            min_size=0,
            max_size=min(8, len(payload) - 1),
            unique=True,
        ),
    )
    chunks = _split_at(payload, cut_points)
    # Split multibyte sequences merge in the decoder, so the write count is
    # the number of chunks that yield decoded text, not the number of bytes.
    # Drawing the failing position over that exact range guarantees the
    # rejection lands on a real chunk write.
    writes = _count_text_writes(chunks)
    failing_write = draw(st.integers(min_value=1, max_value=writes))
    return payload, chunks, failing_write


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


class _FailingWriteSink:
    """Recording sink failing with UnicodeEncodeError at a chosen write."""

    def __init__(self, fail_on_write: int) -> None:
        """Store the 1-based write index that must fail."""
        self._fail_on_write = fail_on_write
        self.attempts = 0
        self.rejected_attempt: int | None = None

    def write(self, payload: str) -> int:
        """Record the attempt and fail once the index is reached."""
        self.attempts += 1
        if self.rejected_attempt is not None:
            msg = "the echo guard must stop further writes"
            raise AssertionError(msg)
        if self.rejected_attempt is None and self.attempts >= self._fail_on_write:
            self.rejected_attempt = self.attempts
            raise UnicodeEncodeError(
                "cp1252",
                payload,
                0,
                1,
                "character maps to <undefined>",
            )
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_echo_guard_case())
def test_echo_guard_preserves_capture_across_arbitrary_chunks(
    case: tuple[bytes, tuple[bytes, ...], int],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Property: one encode failure disables echo exactly once per drain.

    For every payload, chunk partition, and failing write position, capture
    must stay complete, no write may follow the first rejection, and exactly
    one structured warning must record the transition.
    """
    payload, chunks, failing_write = case
    sink = _FailingWriteSink(failing_write)
    relay_diagnostics = _RelayDiagnostics()

    # Hypothesis reuses the fixture across examples, so clear the records the
    # previous example left behind before asserting on this one's drain.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="cuprum.stream"):
        captured = asyncio.run(
            _drain(
                _reader(chunks),
                _config(typ.cast("typ.IO[str]", sink)),
                relay_diagnostics=relay_diagnostics,
            ),
        )
    relay_diagnostics.settle()
    fallbacks = relay_diagnostics.snapshot()
    warnings = [record for record in caplog.records if record.name == "cuprum.stream"]

    assert captured == payload.decode("utf-8", errors="replace"), (
        "capture must decode the complete payload for "
        f"payload={payload!r}, chunks={chunks!r}, failing_write={failing_write!r}"
    )
    assert sink.rejected_attempt is not None, (
        f"the sink must reject one of the chunk writes for chunks={chunks!r}, "
        f"failing_write={failing_write!r}, attempts={sink.attempts!r}"
    )
    assert sink.attempts == sink.rejected_attempt, (
        "no write may follow the first rejection for "
        f"chunks={chunks!r}, failing_write={failing_write!r}, "
        f"attempts={sink.attempts!r}, rejected={sink.rejected_attempt!r}"
    )
    assert len(warnings) == 1, (
        "exactly one disable warning must be logged for "
        f"warnings={warnings!r}, chunks={chunks!r}, failing_write={failing_write!r}"
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
    assert fields["cuprum_error_category"] == "unicode_encode"
