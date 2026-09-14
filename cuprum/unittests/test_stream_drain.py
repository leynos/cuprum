"""Unit properties for the canonical stream-drain helper.

The public pipeline tests cover ``_drain`` through real subprocess I/O.  This
module keeps a small direct contract around the canonical helper itself so the
two ``_consume_stream`` variants cannot diverge silently during refactors.
It uses a stub ``asyncio.StreamReader`` shape to provide deterministic chunk
boundaries, including split UTF-8 sequences and invalid byte payloads.
"""

from __future__ import annotations

import asyncio
import io
import typing as typ

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from cuprum._streams import _consume_stream, _drain, _StreamConfig
from cuprum.echo_events import EchoErrorCategory
from cuprum.echo_observation import observe_echo

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_PROPERTY_MAX_EXAMPLES = 24


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
    capture: bool = True,
    echo: bool = False,
    max_line_bytes: int | None = None,
) -> _StreamConfig:
    """Build a UTF-8 stream config for direct drain tests."""
    return _StreamConfig(
        capture_output=capture,
        echo_output=echo,
        echo_max_line_bytes=max_line_bytes,
        sink=sink,
        encoding="utf-8",
        errors="replace",
    )


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


def _decode_chunks(chunks: cabc.Sequence[bytes]) -> str:
    """Decode chunks as one stream for comparison with text-sink echoing."""
    return b"".join(chunks).decode("utf-8", errors="replace")


def _expected_emitted_lines(payload: bytes) -> list[str]:
    """Model stream line emission while preserving non-CR/LF boundaries."""
    text = payload.decode("utf-8", errors="replace")
    lines: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        match text[index]:
            case "\n":
                lines.append(text[start:index])
                start = index + 1
            case "\r":
                lines.append(text[start:index])
                if index + 1 < len(text) and text[index + 1] == "\n":
                    index += 1
                start = index + 1
        index += 1
    if start < len(text):
        lines.append(text[start:])
    return lines


def test_drain_empty_capture_returns_empty_text() -> None:
    """Empty captured streams drain to an empty string."""
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(()), _config(sink)))

    assert captured == "", "empty captured streams must decode to empty text"
    assert sink.getvalue() == "", "non-echoing empty streams must not write to sink"


def test_drain_respects_capture_and_echo_flags() -> None:
    """Capture and echo flags independently control drain side effects."""
    chunks = (b"alpha", " \u2603".encode())
    sink = io.StringIO()
    captured = asyncio.run(_drain(_reader(chunks), _config(sink, echo=True)))

    assert captured == b"alpha \xe2\x98\x83".decode(), (
        f"captured text must decode the complete payload for chunks={chunks!r}"
    )
    assert sink.getvalue() == _decode_chunks(chunks), (
        f"echo sink must receive every chunk for chunks={chunks!r}"
    )


def test_drain_can_disable_capture_while_echoing() -> None:
    """Echo-only drains write chunks but return no captured text."""
    chunks = (b"only ", b"echo")
    sink = io.StringIO()
    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=False, echo=True)),
    )

    assert captured is None, "capture-disabled drains must return None"
    assert sink.getvalue() == "only echo", (
        f"echo-only drain must write all decoded chunks for chunks={chunks!r}"
    )


def test_discarding_a_cancelled_capture_skips_decoding() -> None:
    """Cleanup cancellation discards an incomplete capture without decoding it."""

    async def run_case() -> None:
        """Cancel a reader after it has buffered invalid but incomplete UTF-8."""
        discard_on_cancel = asyncio.Event()
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xff")
        config = _StreamConfig(
            capture_output=True,
            echo_output=False,
            sink=io.StringIO(),
            encoding="utf-8",
            errors="strict",
            discard_on_cancel=discard_on_cancel,
        )
        task = asyncio.create_task(_drain(reader, config))
        await asyncio.sleep(0)
        discard_on_cancel.set()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_case())


def test_cancelled_capture_retains_buffered_text() -> None:
    """Cancellation returns buffered capture when cleanup does not discard it."""

    async def run_case() -> str | None:
        """Cancel a reader after it buffers text and blocks awaiting EOF."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"partial output")
        task = asyncio.create_task(_drain(reader, _config(io.StringIO())))
        await asyncio.sleep(0)
        task.cancel()
        return await task

    assert asyncio.run(run_case()) == "partial output", (
        "cancellation must return the buffered partial capture"
    )


def test_cancelled_capture_flushes_replacement_echo() -> None:
    """Cancellation flushes an incomplete echoed character before returning it."""

    async def run_case() -> tuple[str | None, str]:
        """Cancel after buffering an incomplete UTF-8 sequence without EOF."""
        reader = asyncio.StreamReader()
        reader.feed_data(b"\xc3")
        sink = io.StringIO()
        task = asyncio.create_task(_drain(reader, _config(sink, echo=True)))
        await asyncio.sleep(0)
        task.cancel()
        return await task, sink.getvalue()

    assert asyncio.run(run_case()) == (
        "\N{REPLACEMENT CHARACTER}",
        "\N{REPLACEMENT CHARACTER}",
    ), "cancellation must flush the replacement character to capture and echo"


def test_line_observer_cancellation_propagates() -> None:
    """Cancellation raised by a line observer is not mistaken for reader cleanup."""

    def cancel_on_line(_line: str) -> None:
        """Model an observer that requests cancellation while receiving output."""
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _consume_stream(
                _reader((b"line\n",)),
                _config(io.StringIO()),
                on_line=cancel_on_line,
            )
        )


def test_drain_echoes_split_multibyte_text_sink() -> None:
    """Text-sink echoing preserves split characters and flushes decoder tails."""
    chunks = (b"prefix \xe2", b"\x98\x83 suffix \xf0\x9f")
    expected = _decode_chunks(chunks)
    sink = io.StringIO()

    captured = asyncio.run(_drain(_reader(chunks), _config(sink, echo=True)))

    assert captured == expected, (
        f"capture must decode the whole payload for chunks={chunks!r}"
    )
    assert sink.getvalue() == expected, (
        "text-sink echo must preserve split multibyte characters and flush "
        f"incomplete decoder tails for chunks={chunks!r}, expected={expected!r}"
    )


def test_drain_strictly_echoes_split_multibyte_text_sink() -> None:
    """Echo-only drains preserve valid UTF-8 split across text-sink reads."""
    chunks = (b"before \xe2", b"\x98", b"\x83 after")
    sink = io.StringIO()
    config = _StreamConfig(
        capture_output=False,
        echo_output=True,
        sink=sink,
        encoding="utf-8",
        errors="strict",
    )

    captured = asyncio.run(_drain(_reader(chunks), config))

    assert captured is None, f"echo-only drains must return None for chunks={chunks!r}"
    assert sink.getvalue() == "before \u2603 after", (
        "strict text-sink echo must reconstruct split multibyte characters for "
        f"chunks={chunks!r}, received={sink.getvalue()!r}"
    )


def test_drain_echoes_original_bytes_to_buffered_sink() -> None:
    """Buffered echo sinks receive original chunks without text decoding."""
    chunks = (b"before \xe2", b"\x98", b"\x83 after")
    raw_sink = io.BytesIO()
    sink = io.TextIOWrapper(raw_sink, encoding="utf-8")

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, capture=False, echo=True)),
    )

    assert captured is None, (
        f"buffered echo-only drains must return None for chunks={chunks!r}"
    )
    assert raw_sink.getvalue() == b"".join(chunks), (
        "buffered echo must preserve original bytes for "
        f"chunks={chunks!r}, received={raw_sink.getvalue()!r}"
    )


def test_drain_truncates_oversized_echo_line_for_text_sink() -> None:
    """A multi-chunk oversized line is bounded in echo but whole in capture."""
    # The acceptance case: a 2 MiB single line must echo under the bound so a
    # bounded consumer (GitHub Actions job logs stop at 64 KiB per line) keeps
    # working, while capture still holds every byte.
    bound = 65536
    payload = b"q" * (2 * 1024 * 1024)
    chunks = tuple(
        payload[start : start + 8192] for start in range(0, len(payload), 8192)
    )
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, echo=True, max_line_bytes=bound)),
    )

    echoed = sink.getvalue()
    assert captured == payload.decode(), (
        "capture must keep the full oversized line regardless of the echo bound"
    )
    prefix, marker = echoed.rsplit("… [truncated ", maxsplit=1)
    dropped = int(marker.removesuffix(" bytes]"))
    assert dropped == len(payload) - len(prefix.encode()), (
        "marker dropped="
        f"{dropped} must match omitted bytes for prefix length={len(prefix)}"
    )
    assert len(echoed.encode()) <= bound, (
        f"echoed line must not exceed bound={bound}, got {len(echoed.encode())}"
    )


def test_drain_truncates_each_line_independently() -> None:
    """Each line restarts the bound; markers report per-line dropped bytes."""
    bound = 50
    payload = b"a" * 100 + b"\n" + b"b" * 20 + b"\n" + b"c" * 80 + b"\n"
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(
            _reader((payload,)),
            _config(sink, echo=True, max_line_bytes=bound),
        ),
    )

    assert captured == payload.decode(), "capture must stay byte-for-byte complete"
    echoed_lines = sink.getvalue().split("\n")
    assert "… [truncated " in echoed_lines[0], (
        "the first oversized line must report omitted bytes"
    )
    assert echoed_lines[1] == "b" * 20, "a line under the bound must echo whole"
    assert "… [truncated " in echoed_lines[2], "the bound must restart for each line"
    assert all(len(line.encode()) <= bound for line in echoed_lines[:-1]), (
        "every mirrored line must include its marker in "
        f"bound={bound}: {echoed_lines!r}"
    )


def test_drain_truncates_unterminated_trailing_line_at_eof() -> None:
    """A trailing partial line is bounded and marked before the drain ends."""
    bound = 50
    payload = b"z" * 80
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader((payload,)), _config(sink, echo=True, max_line_bytes=bound)),
    )

    assert captured == payload.decode(), "capture keeps the unterminated line"
    echoed = sink.getvalue()
    assert "… [truncated " in echoed, (
        f"EOF finalization must mark the truncated partial line, got={echoed!r}"
    )
    assert len(echoed.encode()) <= bound, (
        f"unterminated echoed line must fit bound={bound}, got={len(echoed.encode())}"
    )


def test_echo_only_unterminated_line_keeps_a_bounded_mirror() -> None:
    """capture=False does not require retaining an oversized unfinished line."""
    bound = 50
    payload = b"z" * (2 * 1024 * 1024)
    chunks = tuple(
        payload[start : start + 8192] for start in range(0, len(payload), 8192)
    )
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(
            _reader(chunks),
            _config(sink, capture=False, echo=True, max_line_bytes=bound),
        ),
    )

    assert captured is None, "echo-only drains must not create captured output"
    assert len(sink.getvalue().encode()) <= bound, (
        "the unfinished echoed line must remain bounded without capture"
    )


def test_drain_truncates_multi_byte_utf8_straddling_the_bound() -> None:
    """A cut inside a multi-byte sequence keeps the echoed text decodable."""
    snowman = "☃".encode()
    # The marker leaves two source bytes, so the bound falls mid-character.
    bound = 28
    payload = b"ab" + snowman + b"c" * 35 + b"\n"
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader((payload,)), _config(sink, echo=True, max_line_bytes=bound)),
    )

    assert captured == payload.decode(), "capture keeps the full line"
    echoed_line = sink.getvalue().removesuffix("\n")
    assert "… [truncated " in echoed_line, (
        f"the marker must report omitted bytes, got={echoed_line!r}"
    )
    assert echoed_line.startswith("ab…"), (
        "strict-safe truncation must omit a split character rather than decode "
        f"a replacement prefix, got={echoed_line!r}"
    )


def test_drain_echoes_raw_bytes_when_line_bound_is_none() -> None:
    """``echo_max_line_bytes=None`` restores chunk-for-chunk mirroring."""
    chunks = (b"a" * 100 + b"\n", b"b" * 80)
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, echo=True, max_line_bytes=None)),
    )

    assert captured == b"".join(chunks).decode(), (
        "capture must stay complete when echoing is unbounded"
    )
    assert sink.getvalue() == b"".join(chunks).decode(), (
        "unbounded echo must mirror the payload byte-for-byte"
    )


def test_drain_truncates_for_byte_buffered_sink() -> None:
    """Buffered byte sinks receive raw kept bytes and an encoded marker."""
    bound = 50
    payload = b"x" * 80 + b"\n"
    raw_sink = io.BytesIO()
    sink = io.TextIOWrapper(raw_sink, encoding="utf-8")

    captured = asyncio.run(
        _drain(_reader((payload,)), _config(sink, echo=True, max_line_bytes=bound)),
    )

    assert captured == payload.decode(), "capture must stay byte-for-byte complete"
    echoed = raw_sink.getvalue()
    assert len(echoed) <= bound, (
        f"buffered echoed line must fit bound={bound}, got={len(echoed)}"
    )
    assert b"truncated " in echoed, "buffered echo must include a truncation marker"


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


def test_rejected_bounded_echo_does_not_observe_truncation() -> None:
    """A sink-rejected bounded write emits only its encoding-failure event."""
    sink = _Cp1252TextOnlySink()
    events = []
    payload = "ś".encode() + b"x" * 80 + b"\n"
    config = _StreamConfig(
        capture_output=True,
        echo_output=True,
        echo_max_line_bytes=50,
        sink=typ.cast("typ.IO[str]", sink),
        encoding="utf-8",
        errors="strict",
    )

    with observe_echo(events.append):
        captured = asyncio.run(_drain(_reader((payload,)), config))

    assert captured == payload.decode(), "capture must retain the rejected line"
    assert [event.error_category for event in events] == [
        EchoErrorCategory.UNICODE_ENCODE
    ], f"only the failed write must be observed, got={events!r}"
    assert len(sink.attempts) == 1, (
        "disabled echo must not retry later writes or the final decoder flush"
    )


def test_bounded_echo_writes_untruncated_line_in_one_payload() -> None:
    """An untruncated line on the bounded path is mirrored in a single write."""
    bound = 50
    payload = b"well within the bound\n"
    sink = _RecordingSink()

    captured = asyncio.run(
        _drain(
            _reader((payload,)),
            _config(typ.cast("typ.IO[str]", sink), echo=True, max_line_bytes=bound),
        ),
    )

    assert captured == payload.decode(), "capture must stay byte-for-byte complete"
    assert sink.writes == ["well within the bound\n"], (
        "an untruncated line must arrive as one write including its terminator"
    )


class _RecordingSink:
    """Text sink that records each write payload in order."""

    def __init__(self) -> None:
        """Start with an empty write log."""
        self.writes: list[str] = []

    def write(self, payload: str) -> int:
        """Record and accept the payload."""
        self.writes.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


def test_final_flush_after_disabled_echo_writes_nothing() -> None:
    """A disabled echo never re-attempts the final decoder flush write."""
    sink = _Cp1252TextOnlySink()
    captured = asyncio.run(
        _drain(
            _reader(("ś".encode(), b"\xc3")),
            _config(typ.cast("typ.IO[str]", sink), echo=True),
        ),
    )
    attempts = sink.attempts

    assert captured == "ś\N{REPLACEMENT CHARACTER}", (
        "capture must keep the rejected character and flush the decoder tail "
        f"after echo is disabled for captured={captured!r}"
    )
    assert attempts == ["ś"], (
        "exactly one write must be attempted before echo is disabled, and the "
        f"final decoder flush must stay silent for attempts={attempts!r}"
    )


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_payload_and_chunks())
def test_consume_stream_variants_capture_identically(
    case: tuple[bytes, tuple[bytes, ...]],
) -> None:
    """Property: line and plain consume variants capture identical text."""
    payload, chunks = case
    plain_sink = io.StringIO()
    line_sink = io.StringIO()

    plain = asyncio.run(_consume_stream(_reader(chunks), _config(plain_sink)))
    with_lines = asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(line_sink),
            on_line=lambda _line: None,
        ),
    )

    assert plain == with_lines == payload.decode("utf-8", errors="replace"), (
        "plain and line-emitting variants must match whole-payload decode for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@example(case=(b"first\nsecond\n", (b"first\nsec", b"ond\n")))
@given(case=_payload_and_chunks())
def test_line_emission_is_chunk_boundary_insensitive(
    case: tuple[bytes, tuple[bytes, ...]],
) -> None:
    """Property: line emission is independent of stream chunk boundaries."""
    payload, chunks = case
    expected_lines = _expected_emitted_lines(payload)
    whole_lines: list[str] = []
    split_lines: list[str] = []

    asyncio.run(
        _consume_stream(
            _reader((payload,) if payload else ()),
            _config(io.StringIO()),
            on_line=whole_lines.append,
        ),
    )
    asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(io.StringIO()),
            on_line=split_lines.append,
        ),
    )

    assert whole_lines == expected_lines, (
        "whole-payload line emission must match decoded payload lines for "
        f"payload={payload!r}, chunks={chunks!r}, expected={expected_lines!r}"
    )
    assert split_lines == expected_lines, (
        "split-payload line emission must match decoded payload lines for "
        f"payload={payload!r}, chunks={chunks!r}, expected={expected_lines!r}"
    )
    assert split_lines == whole_lines, (
        "line emission must not depend on chunk boundaries for "
        f"payload={payload!r}, chunks={chunks!r}"
    )


@pytest.mark.parametrize("terminated", [True, False])
def test_strict_utf8_bounded_echo_omits_a_split_character(
    terminated: bool,
) -> None:
    """Strict text echo completes when the byte budget falls inside UTF-8."""
    payload = b"ab" + "☃".encode() + b"c" * 35 + (b"\n" if terminated else b"")
    sink = io.StringIO()
    config = _StreamConfig(
        capture_output=True,
        echo_output=True,
        echo_max_line_bytes=28,
        sink=sink,
        encoding="utf-8",
        errors="strict",
    )

    captured = asyncio.run(_drain(_reader((payload[:3], payload[3:])), config))

    assert captured == payload.decode(), "capture must retain the original UTF-8 line"
    assert sink.getvalue().startswith("ab"), "echo must retain only the safe prefix"
    assert "☃" not in sink.getvalue(), "echo must not write a split character"
    assert len(sink.getvalue().encode()) <= 28, (
        "echo must include marker and ending in bound"
    )


def test_bounded_echo_recognizes_crlf_split_across_chunks() -> None:
    """A pending CR becomes a CRLF terminator when the next chunk starts with LF."""
    payload = b"x" * 40 + b"\r\n"
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(
            _reader((payload[:-1], payload[-1:])),
            _config(sink, echo=True, max_line_bytes=42),
        ),
    )

    assert captured == payload.decode(), "capture must preserve split CRLF bytes"
    assert sink.getvalue() == payload.decode(), "exactly bounded CRLF must not truncate"


def test_bounded_echo_marks_over_limit_split_crlf_before_terminator() -> None:
    """A split CRLF does not consume body budget and follows the marker."""
    payload = b"x" * 80 + b"\r\n"
    sink = io.StringIO()

    captured = asyncio.run(
        _drain(
            _reader((payload[:-1], payload[-1:])),
            _config(sink, echo=True, max_line_bytes=50),
        ),
    )

    echoed = sink.getvalue()
    assert captured == payload.decode(), "capture must preserve the oversized CRLF line"
    assert echoed.endswith("\r\n"), "marker must precede the complete CRLF terminator"
    assert "truncated " in echoed, "over-limit CRLF line must report truncation"
    assert len(echoed.encode()) <= 50, "CRLF must be included in the line bound"


def test_bounded_echo_keeps_a_standalone_trailing_carriage_return() -> None:
    """A CR without a following LF remains child output at EOF."""
    sink = io.StringIO()
    payload = b"body\r"

    captured = asyncio.run(
        _drain(_reader((payload,)), _config(sink, echo=True, max_line_bytes=50))
    )

    assert captured == payload.decode(), "capture must preserve a standalone CR"
    assert sink.getvalue() == payload.decode(), "EOF must echo a standalone CR as body"


def test_bounded_echo_keeps_cr_before_a_non_lf_byte_as_body() -> None:
    """A pending CR is replayed as body data when the next byte is not LF."""
    sink = io.StringIO()
    chunks = (b"body\r", b"next\n")

    captured = asyncio.run(
        _drain(_reader(chunks), _config(sink, echo=True, max_line_bytes=50))
    )

    assert captured == "body\rnext\n", "capture must retain the CR and following text"
    assert sink.getvalue() == "body\rnext\n", "echo must preserve standalone CR body"


def test_bounded_echo_observes_successful_truncation() -> None:
    """A successful truncated write emits its stream and omitted-byte count."""
    events = []
    sink = io.StringIO()

    with observe_echo(events.append):
        captured = asyncio.run(
            _drain(
                _reader((b"x" * 80 + b"\n",)),
                _config(sink, echo=True, max_line_bytes=50),
            )
        )

    assert captured == "x" * 80 + "\n", "capture must stay complete"
    assert len(events) == 1, f"one truncated line must emit one event, got={events!r}"
    assert events[0].error_category is EchoErrorCategory.TRUNCATED, (
        f"event must use the closed truncation category, got={events[0]!r}"
    )
    dropped_bytes = events[0].dropped_bytes
    assert dropped_bytes is not None, (
        f"event must report an omitted-byte count, got={events[0]!r}"
    )
    assert dropped_bytes > 0, (
        f"event must report omitted child bytes, got={events[0]!r}"
    )


@pytest.mark.parametrize(
    ("encoding", "errors"),
    [("ascii", "replace"), ("latin-1", "replace"), ("ascii", "strict")],
)
def test_bounded_text_echo_uses_a_representable_marker(
    encoding: str,
    errors: str,
) -> None:
    """Text sinks with narrow encodings truncate without aborting capture."""
    sink = io.StringIO()
    payload = b"x" * 80 + b"\n"
    config = _StreamConfig(
        capture_output=True,
        echo_output=True,
        echo_max_line_bytes=50,
        sink=sink,
        encoding=encoding,
        errors=errors,
    )

    captured = asyncio.run(_drain(_reader((payload,)), config))

    assert captured == payload.decode(), f"{encoding} capture must remain complete"
    assert "... [truncated " in sink.getvalue(), (
        f"{encoding} must use the ASCII fallback marker, got={sink.getvalue()!r}"
    )
    assert len(sink.getvalue().encode(encoding, errors)) <= 50, (
        f"{encoding} echo must fit its byte bound"
    )
