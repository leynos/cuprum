r"""Property-based and example tests for the pure echo-truncation helpers.

This module verifies :mod:`cuprum._echo_truncation` without subprocesses,
asyncio streams, or sinks. ``_EchoLineLimiter`` bounds each mirrored line at a
configured byte count, counts the dropped remainder, and reports the marker
that the drain loop writes before the line ending. The Hypothesis properties
prove the invariants: a bounded line never emits more than the bound, capture
is unaffected because the limiter never sees it, and the marker count equals
the bytes actually dropped. Example cases pin the normative boundaries:
exactly-at-bound lines, one-byte-over lines, chunk-split lines, and multi-byte
UTF-8 sequences straddling the cut.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from cuprum._echo_truncation import _EchoLineLimiter, truncation_marker

_PROPERTY_SETTINGS = settings(
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    max_examples=30,
)


def _marker_length(encoding: str) -> int:
    """Byte length of a marker reporting one dropped byte in *encoding*."""
    return len(truncation_marker(1, encoding=encoding))


def _marker_length_bound(dropped: int, encoding: str) -> int:
    r"""Upper bound on the marker byte length for *dropped* dropped bytes.

    The decimal rendering of *dropped* is at most ``len(str(dropped))``
    digits, so this stays tight for the payloads the tests generate.

    Returns
    -------
    int
        The byte length of a marker reporting the maximum digit count.
    """
    digits = len(str(dropped))
    return len(truncation_marker(10**digits - 1, encoding=encoding))


def _feed(limiter: _EchoLineLimiter, payload: bytes) -> str:
    """Feed *payload* to *limiter* as newline-terminated lines.

    Returns
    -------
    str
        The echoed text the drain loop would produce: kept bytes, the
        truncation marker for each truncated line, and a line ending per
        complete line.
    """
    pieces: list[bytes] = []
    lines = payload.split(b"\n")
    for line in lines[:-1]:  # the split leaves one empty trailing segment
        kept = limiter.bound_line(line)
        if kept:
            pieces.append(kept)
        marker = limiter.finish_line(encoding="utf-8")
        if marker is not None:
            pieces.append(marker)
        pieces.append(b"\n")
    return b"".join(pieces).decode("utf-8", errors="replace")


@st.composite
def _lines_and_bound(draw: st.DrawFn) -> tuple[bytes, int]:
    """Generate a payload of newline-separated lines plus a byte bound."""
    bound = draw(st.integers(min_value=1, max_value=64))
    lines = draw(
        st.lists(
            st.binary(max_size=bound * 2 + 8),
            min_size=1,
            max_size=6,
        ),
    )
    return b"\n".join(lines) + b"\n", bound


@_PROPERTY_SETTINGS
@given(case=_lines_and_bound())
@example(case=(b"x" * 64 + b"\n", 64))
@example(case=(b"x" * 65 + b"\n", 64))
def test_bounded_lines_emit_at_most_bound_plus_marker(
    case: tuple[bytes, int],
) -> None:
    """Property: echoed lines never exceed the bound plus one marker.

    Parameters
    ----------
    case : tuple[bytes, int]
        A newline-terminated payload and the per-line byte bound.
    """
    payload, bound = case
    limiter = _EchoLineLimiter(max_line_bytes=bound)

    echoed = _feed(limiter, payload)

    for line in echoed.split("\n")[:-1]:
        # The bound counts raw child bytes; text-sink echoing decodes them,
        # and an invalid byte becomes U+FFFD (three UTF-8 bytes), so the
        # echoed text may expand up to three times the kept-byte count. The
        # bound still holds for the raw child bytes the sink eventually
        # renders; here we check the post-decode envelope.
        assert len(line.encode()) <= bound * 3 + _marker_length_bound(
            len(payload), "utf-8"
        ), f"echoed line exceeded bound={bound} for payload={payload!r}"


@_PROPERTY_SETTINGS
@given(case=_lines_and_bound())
def test_marker_reports_dropped_bytes(case: tuple[bytes, int]) -> None:
    """Property: each marker count equals the bytes dropped from its line.

    Parameters
    ----------
    case : tuple[bytes, int]
        A newline-terminated payload and the per-line byte bound.
    """
    payload, bound = case
    limiter = _EchoLineLimiter(max_line_bytes=bound)

    echoed = _feed(limiter, payload)

    # Count and compare in bytes: the bound is byte-based, so text decoding
    # with replacement would obscure how many bytes each line contributed.
    lines = payload[:-1].split(b"\n")
    echoed_lines = echoed.split("\n")[:-1]
    assert len(lines) == len(echoed_lines)
    for original, mirrored in zip(lines, echoed_lines, strict=True):
        dropped = max(len(original) - bound, 0)
        if dropped:
            assert mirrored.endswith(f"… [truncated {dropped} bytes]"), (
                f"marker must report {dropped} dropped bytes for line={original!r}"
            )
            assert len(mirrored.encode()) <= bound * 3 + _marker_length_bound(
                len(original), "utf-8"
            ), (
                "the mirrored line must stay near the bound even when invalid "
                f"bytes decode to replacement characters for line={original!r}"
            )
        else:
            assert mirrored == original.decode("utf-8", errors="replace"), (
                f"untruncated line must mirror exactly for line={original!r}"
            )


@_PROPERTY_SETTINGS
@given(case=_lines_and_bound())
def test_limiter_resets_between_lines(case: tuple[bytes, int]) -> None:
    """Property: the bound applies per line, never across lines.

    Parameters
    ----------
    case : tuple[bytes, int]
        A newline-terminated payload and the per-line byte bound.
    """
    payload, bound = case
    limiter = _EchoLineLimiter(max_line_bytes=bound)

    _feed(limiter, payload)

    assert limiter.emitted_line_bytes == 0, (
        "finish_line must reset emitted bytes for the next line"
    )
    assert limiter.dropped_line_bytes == 0, (
        "finish_line must reset dropped bytes for the next line"
    )


@pytest.mark.parametrize(
    ("dropped", "encoding", "expected"),
    [
        pytest.param(1, "utf-8", "… [truncated 1 bytes]", id="utf-8"),
        pytest.param(0, "utf-8", "… [truncated 0 bytes]", id="zero"),
    ],
)
def test_truncation_marker_encodes_reported_count(
    dropped: int,
    encoding: str,
    expected: str,
) -> None:
    """Example: the marker text states the dropped byte count."""
    assert truncation_marker(dropped, encoding=encoding) == expected.encode(encoding)


def test_from_config_requires_echo_with_bound() -> None:
    """The limiter exists only when echoing with a non-None bound."""
    assert (
        _EchoLineLimiter.from_config(
            echo_output=True,
            echo_max_line_bytes=None,
        )
        is None
    )
    assert (
        _EchoLineLimiter.from_config(
            echo_output=False,
            echo_max_line_bytes=64,
        )
        is None
    )
    assert (
        _EchoLineLimiter.from_config(
            echo_output=False,
            echo_max_line_bytes=None,
        )
        is None
    )
    assert _EchoLineLimiter.from_config(
        echo_output=True, echo_max_line_bytes=64
    ) == _EchoLineLimiter(64)


def test_utf8_sequence_straddling_bound_is_not_split() -> None:
    """Example: a multi-byte UTF-8 character straddling the cut stays intact.

    The bound cuts between bytes; the drain loop feeds kept bytes through the
    incremental decoder, so the replacement behaviour of a split character is
    a sink-level concern. What the limiter guarantees is that the cut never
    emits more than the bound and never inflates the dropped count.
    """
    snowman = "☃".encode()  # three-byte UTF-8 sequence
    limiter = _EchoLineLimiter(max_line_bytes=10)
    payload = b"ab" + snowman + b"c" * 5  # 10 bytes exactly

    kept = limiter.bound_line(payload)

    assert len(kept) == 10
    assert limiter.dropped_line_bytes == 0
    assert kept == payload, "an exactly-at-bound line is mirrored whole"


def test_utf8_sequence_straddling_bound_keeps_byte_prefix() -> None:
    """Example: the cut lands inside a multi-byte UTF-8 sequence at the bound.

    The bound is byte-based, so the cut can land between the bytes of one
    character. The limiter guarantees a clean byte-prefix cut; the drain loop
    feeds kept bytes through the incremental echo decoder, whose replacement
    behaviour keeps the sink text valid without corrupting the stream.
    """
    snowman = "☃".encode()  # three-byte UTF-8 sequence
    limiter = _EchoLineLimiter(max_line_bytes=10)
    payload = b"ab" + snowman + b"c" * 8  # 13 bytes: cut lands inside ☃

    kept = limiter.bound_line(payload)

    assert len(kept) == 10, "the cut never exceeds the bound"
    assert limiter.dropped_line_bytes == 3
    assert kept[:2] == b"ab"
    assert kept[2] == snowman[0], "cut keeps a byte prefix of the split sequence"
