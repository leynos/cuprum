"""Unit contracts for bounded mirrored-line assembly."""

from __future__ import annotations

import dataclasses as dc

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._echo_truncation import (
    _EchoEncoding,
    _EchoLineLimiter,
    _encode_prefix,
    _FinishedEchoLine,
    truncation_marker,
)


@dc.dataclass(frozen=True)
class _FinishOptions:
    """Options for one bounded-line finalization."""

    ending: bytes = b"\n"
    encoding: str = "utf-8"
    errors: str = "strict"
    is_text_sink: bool = False


_DEFAULT_FINISH_OPTIONS = _FinishOptions()


def _finish(
    line: bytes,
    *,
    bound: int,
    options: _FinishOptions = _DEFAULT_FINISH_OPTIONS,
) -> _FinishedEchoLine:
    """Build one finished bounded echo line."""
    limiter = _EchoLineLimiter(bound)
    limiter.bound_line(line)
    return limiter.finish_line(
        ending=options.ending,
        encoding=options.encoding,
        errors=options.errors,
        is_text_sink=options.is_text_sink,
    )


def test_utf8_marker_keeps_the_ellipsis() -> None:
    """UTF-8 uses the established ellipsis marker."""
    assert truncation_marker(10, encoding="utf-8", errors="strict") == (
        "… [truncated 10 bytes]".encode()
    ), "UTF-8 must retain the established truncation marker"


@pytest.mark.parametrize("encoding", ["ascii", "latin-1"])
def test_unrepresentable_marker_uses_ascii_fallback(encoding: str) -> None:
    """Single-byte encodings receive a representable marker without failure."""
    marker = truncation_marker(10, encoding=encoding, errors="replace")

    assert marker == b"... [truncated 10 bytes]", (
        f"{encoding} must receive the ASCII fallback marker, got={marker!r}"
    )


@given(
    line=st.binary(min_size=1, max_size=256),
    bound=st.integers(min_value=1, max_value=128),
)
def test_finished_byte_sink_line_never_exceeds_its_bound(
    line: bytes,
    bound: int,
) -> None:
    """Every completed byte-sink line includes its marker and ending in the bound."""
    finished = _finish(line, bound=bound)

    assert len(finished.payload) <= bound, (
        f"payload length={len(finished.payload)} exceeded bound={bound}"
    )
    assert finished.dropped_bytes >= 0, (
        f"dropped byte count must be non-negative, got={finished.dropped_bytes}"
    )


def test_one_byte_over_the_limit_reserves_marker_and_ending() -> None:
    """One extra child byte never makes the mirrored line exceed its bound."""
    finished = _finish(b"x" * 41, bound=40)

    assert len(finished.payload) <= 40, (
        f"one-byte overflow produced {len(finished.payload)} bytes"
    )
    assert finished.dropped_bytes > 0, "one-byte overflow must omit child bytes"


def test_truncated_payload_keeps_original_source_byte_count() -> None:
    """Delivery accounting excludes marker bytes added to the echoed payload."""
    source = b"x" * 80
    finished = _finish(source, bound=40)

    assert finished.source_bytes == len(source) + 1
    assert len(finished.payload) + finished.dropped_bytes > finished.source_bytes


@pytest.mark.parametrize(
    ("line", "expected"),
    [(b"x" * 273, 98), (b"x" * 274, 100)],
)
def test_marker_recalculates_at_digit_width_transitions(
    line: bytes,
    expected: int,
) -> None:
    """The marker budget is recalculated when dropped bytes grow another digit."""
    finished = _finish(line, bound=200)

    assert finished.dropped_bytes == expected, (
        f"expected {expected} dropped bytes, got={finished.dropped_bytes}"
    )
    assert f"truncated {expected} bytes".encode() in finished.payload, (
        f"marker must report {expected} dropped bytes, got={finished.payload!r}"
    )
    assert len(truncation_marker(10, encoding="utf-8", errors="strict")) == (
        len(truncation_marker(9, encoding="utf-8", errors="strict")) + 1
    ), "the marker must account for the additional digit from 9 to 10"


def test_limit_smaller_than_marker_still_stays_bounded() -> None:
    """A tiny limit truncates the marker itself rather than overflowing a log line."""
    finished = _finish(b"x" * 20, bound=5)

    assert len(finished.payload) <= 5, (
        f"tiny bound yielded {len(finished.payload)} bytes: {finished.payload!r}"
    )
    assert finished.dropped_bytes == 20, (
        f"all source bytes must be counted as dropped, got={finished.dropped_bytes}"
    )


def test_limiter_keeps_only_a_bounded_unterminated_prefix() -> None:
    """Source-byte accounting does not retain an oversized unfinished line."""
    limiter = _EchoLineLimiter(50)
    limiter.bound_line(b"x" * 4096)

    assert limiter._source_line_bytes == 4096, (
        "the limiter must retain the full source-byte count for its marker"
    )
    assert len(limiter._line) == 50, (
        "the limiter must retain no more than the configured echoed prefix"
    )


def test_crlf_larger_than_the_bound_is_counted_as_dropped() -> None:
    """A bound smaller than CRLF never overflows the echoed line."""
    finished = _finish(b"", bound=1, options=_FinishOptions(ending=b"\r\n"))

    assert len(finished.payload) <= 1, (
        f"the CRLF edge case exceeded the one-byte bound: {finished.payload!r}"
    )
    assert finished.dropped_bytes == 2, (
        "the omitted CRLF bytes must be included in the dropped-byte count, "
        f"got={finished.dropped_bytes}"
    )


def test_strict_utf8_text_prefix_never_ends_inside_a_character() -> None:
    """A strict text sink receives only a complete UTF-8 character prefix."""
    line = b"ab" + "☃".encode() + b"c" * 35
    finished = _finish(line, bound=28, options=_FinishOptions(is_text_sink=True))

    text = finished.payload.decode("utf-8", "strict")
    assert text.startswith("ab"), f"expected the safe prefix, got={text!r}"
    assert "☃" not in text, f"split character must be omitted, got={text!r}"
    assert finished.dropped_bytes == len(line) - 2, (
        "moving to a character boundary must count every newly omitted byte"
    )
    assert finished.payload == (b"ab" + "… [truncated 38 bytes]".encode() + b"\n"), (
        f"the exact truncated line changed, got={finished.payload!r}"
    )
    assert finished.source_bytes == len(line) + 1, (
        "source accounting must include the retained terminator"
    )


def test_strict_utf8_text_prefix_keeps_the_longest_valid_boundary() -> None:
    """A complete multi-byte character is retained when it fits the budget."""
    snowman = "☃".encode()
    line = b"ab" + snowman + b"x" * 40
    finished = _finish(line, bound=32, options=_FinishOptions(is_text_sink=True))

    assert finished.payload.startswith(b"ab" + snowman), (
        "the largest valid prefix must retain the complete fitting snowman, "
        f"got={finished.payload!r}"
    )
    assert finished.payload == (
        b"ab" + snowman + b"xx" + "… [truncated 38 bytes]".encode() + b"\n"
    ), f"the exact snowman boundary changed, got={finished.payload!r}"
    assert finished.dropped_bytes == 38, (
        f"expected 38 dropped bytes, got={finished.dropped_bytes}"
    )
    assert finished.source_bytes == len(line) + 1, (
        f"source accounting must include the terminator, got={finished.source_bytes}"
    )


@pytest.mark.parametrize(
    ("bound", "prefix", "dropped"),
    [
        (30, "日本語".encode("shift_jis"), 54),
        (40, "日本語のテキスト".encode("shift_jis"), 44),
    ],
)
def test_shift_jis_text_prefix_keeps_two_byte_ellipsis_marker(
    bound: int,
    prefix: bytes,
    dropped: int,
) -> None:
    """Shift_JIS keeps whole CJK characters and its two-byte ellipsis marker."""
    line = ("日本語のテキストです" * 3).encode("shift_jis")
    finished = _finish(
        line,
        bound=bound,
        options=_FinishOptions(encoding="shift_jis", is_text_sink=True),
    )

    assert finished.payload == (
        prefix + f"… [truncated {dropped} bytes]".encode("shift_jis") + b"\n"
    ), f"the exact Shift_JIS truncation changed, got={finished.payload!r}"
    assert b"\x81c" in finished.payload, (
        "the ellipsis marker must stay the two-byte Shift_JIS sequence"
    )
    assert finished.dropped_bytes == dropped, (
        f"expected {dropped} dropped bytes, got={finished.dropped_bytes}"
    )
    assert finished.source_bytes == len(line) + 1, (
        f"source accounting must include the terminator, got={finished.source_bytes}"
    )


@pytest.mark.parametrize(
    ("errors", "bound", "prefix", "dropped"),
    [
        ("replace", 32, "ab�", 43),
        ("replace", 40, "ab��cdxxxxx", 35),
        ("ignore", 32, "abcdxxx", 37),
        ("ignore", 40, "abcdxxxxxxxxxxx", 29),
    ],
)
def test_invalid_bytes_under_a_lenient_text_policy(
    errors: str,
    bound: int,
    prefix: str,
    dropped: int,
) -> None:
    """Replace and ignore policies keep the same truncation boundary rules."""
    line = b"ab\xff\xfecd" + b"x" * 40
    finished = _finish(
        line,
        bound=bound,
        options=_FinishOptions(errors=errors, is_text_sink=True),
    )

    assert finished.payload == (
        prefix.encode() + f"… [truncated {dropped} bytes]".encode() + b"\n"
    ), f"the exact {errors} truncation changed, got={finished.payload!r}"
    assert finished.dropped_bytes == dropped, (
        f"expected {dropped} dropped bytes, got={finished.dropped_bytes}"
    )
    assert finished.source_bytes == len(line) + 1, (
        f"source accounting must include the terminator, got={finished.source_bytes}"
    )


def test_strict_decode_error_after_a_prefix_keeps_that_prefix() -> None:
    """A strict decoder stops at an undecodable byte but keeps earlier text."""
    line = b"ab\xff\xfecd" + b"x" * 40
    finished = _finish(line, bound=32, options=_FinishOptions(is_text_sink=True))

    assert finished.payload == (b"ab" + "… [truncated 44 bytes]".encode() + b"\n"), (
        "the accepted prefix must survive a strict decode error, "
        f"got={finished.payload!r}"
    )
    assert finished.dropped_bytes == 44, (
        f"expected 44 dropped bytes, got={finished.dropped_bytes}"
    )


def test_retained_buffer_cut_inside_a_multibyte_character_stays_valid() -> None:
    """A limiter cut through a UTF-8 character never reaches the sink."""
    line = b"x" * 30 + "☃".encode() + b"x" * 40
    finished = _finish(line, bound=32, options=_FinishOptions(is_text_sink=True))

    finished.payload.decode("utf-8", "strict")
    assert finished.payload == (b"x" * 7 + "… [truncated 66 bytes]".encode() + b"\n"), (
        f"the cut character must be omitted, got={finished.payload!r}"
    )
    assert finished.dropped_bytes == 66, (
        f"expected 66 dropped bytes, got={finished.dropped_bytes}"
    )


def test_encode_prefix_omits_a_pending_multibyte_tail() -> None:
    """A source prefix ending mid-character never yields a partial encoding."""
    retained, encoded = _encode_prefix(
        b"ab\xe2\x98",
        budget=10,
        settings=_EchoEncoding("utf-8", "strict", is_text_sink=True),
    )

    assert retained == b"ab", f"a pending tail must be omitted, got={retained!r}"
    assert encoded == b"ab", f"the encoding must match the prefix, got={encoded!r}"


_TEXT_ENCODINGS = ("utf-8", "shift_jis", "latin-1", "ascii")
_ERROR_POLICIES = ("strict", "replace", "ignore")


@given(
    line=st.binary(max_size=200),
    budget=st.integers(min_value=0, max_value=200),
    encoding=st.sampled_from(_TEXT_ENCODINGS),
    errors=st.sampled_from(_ERROR_POLICIES),
)
def test_text_sink_prefix_is_bounded_and_on_a_character_boundary(
    line: bytes,
    budget: int,
    encoding: str,
    errors: str,
) -> None:
    """A text-sink prefix fits its budget and encodes whole retained characters."""
    retained, encoded = _encode_prefix(
        line,
        budget=budget,
        settings=_EchoEncoding(encoding, errors, is_text_sink=True),
    )

    assert len(encoded) <= budget, (
        f"{encoding}/{errors} emitted {len(encoded)} bytes for budget {budget}"
    )
    assert line.startswith(retained), (
        f"the retained source must be a prefix of the line, got={retained!r}"
    )
    # Re-encoding the retained text with a whole-string codec is an independent
    # oracle for the incremental scan, including lenient invalid-byte handling.
    assert encoded == retained.decode(encoding, errors).encode(encoding, errors), (
        f"{encoding}/{errors} echoed bytes must encode the retained source text, "
        f"got={encoded!r} for retained={retained!r}"
    )


def test_final_unterminated_line_includes_its_marker_in_the_bound() -> None:
    """EOF finalization applies the same inclusive byte limit without an ending."""
    finished = _finish(b"x" * 80, bound=40, options=_FinishOptions(ending=b""))

    assert len(finished.payload) <= 40, (
        f"unterminated echo length={len(finished.payload)} exceeded 40"
    )
    assert b"truncated " in finished.payload, (
        f"EOF truncation must retain a marker when it fits, got={finished.payload!r}"
    )
