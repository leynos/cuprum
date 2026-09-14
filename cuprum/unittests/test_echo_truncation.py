"""Unit contracts for bounded mirrored-line assembly."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._echo_truncation import (
    _EchoLineLimiter,
    _FinishedEchoLine,
    truncation_marker,
)


def _finish(
    line: bytes,
    *,
    bound: int,
    ending: bytes = b"\n",
    encoding: str = "utf-8",
    errors: str = "strict",
    is_text_sink: bool = False,
) -> _FinishedEchoLine:
    """Build one finished bounded echo line."""
    limiter = _EchoLineLimiter(bound)
    limiter.bound_line(line)
    return limiter.finish_line(
        ending=ending,
        encoding=encoding,
        errors=errors,
        is_text_sink=is_text_sink,
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


def test_crlf_larger_than_the_bound_is_counted_as_dropped() -> None:
    """A bound smaller than CRLF never overflows the echoed line."""
    finished = _finish(b"", bound=1, ending=b"\r\n")

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
    finished = _finish(line, bound=28, is_text_sink=True)

    text = finished.payload.decode("utf-8", "strict")
    assert text.startswith("ab"), f"expected the safe prefix, got={text!r}"
    assert "☃" not in text, f"split character must be omitted, got={text!r}"
    assert finished.dropped_bytes == len(line) - 2, (
        "moving to a character boundary must count every newly omitted byte"
    )


def test_final_unterminated_line_includes_its_marker_in_the_bound() -> None:
    """EOF finalization applies the same inclusive byte limit without an ending."""
    finished = _finish(b"x" * 80, bound=40, ending=b"")

    assert len(finished.payload) <= 40, (
        f"unterminated echo length={len(finished.payload)} exceeded 40"
    )
    assert b"truncated " in finished.payload, (
        f"EOF truncation must retain a marker when it fits, got={finished.payload!r}"
    )
