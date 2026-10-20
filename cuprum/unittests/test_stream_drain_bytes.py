"""Byte-exact capture and decoded line-observation contracts."""

from __future__ import annotations

import asyncio
import io

import pytest

from cuprum._streams import _consume_stream
from cuprum.unittests._stream_drain_support import _byte_config, _config, _reader


def test_byte_exact_echo_survives_a_strict_capture_policy() -> None:
    """Echo renders a view, so its decode cannot end a strict byte run.

    The echo channel decodes the child's bytes for a text-only sink. Reading
    the caller's ``errors="strict"`` here would raise from the drain's read
    loop on the invalid tail, so a run that only asked to be mirrored would
    lose the bytes it had already captured. The capture keeps the caller's
    policy and still hands those bytes back untouched.
    """
    payload = b"first\nsecond\xff\nthird"
    sink = io.StringIO()

    captured = asyncio.run(
        _consume_stream(
            _reader((payload,)),
            _byte_config(sink, echo=True, errors="strict"),
        )
    )

    assert captured == payload, f"the capture must stay byte-exact, got {captured!r}"
    assert sink.getvalue() == "first\nsecond�\nthird", (
        "the mirror must render the replacement view rather than raise, got "
        f"{sink.getvalue()!r}"
    )


def test_text_mode_echo_still_raises_under_a_strict_capture_policy() -> None:
    """A strict text run's echo keeps the caller's policy, base behaviour kept.

    The byte-exact replacement view above is scoped to byte mode. A text run
    that asked to reject undecodable bytes keeps rejecting them, and the echo
    decoder is one of the decodes that promise covers: the drain raises from
    the read loop at the invalid chunk rather than mirroring a replacement the
    caller's policy never sanctioned. The valid prefix arrives first, so the
    sink's contents also prove the strict view echoed what it could vouch for
    and then stopped.
    """
    sink = io.StringIO()

    with pytest.raises(UnicodeDecodeError):
        asyncio.run(
            _consume_stream(
                _reader((b"first line\n", b"second \xff line\n")),
                _config(sink, echo=True, errors="strict"),
            )
        )

    assert sink.getvalue() == "first line\n", (
        "the strict mirror must stop at the undecodable chunk, having written "
        f"only the text it could vouch for, got {sink.getvalue()!r}"
    )


def test_byte_exact_capture_still_observes_decoded_lines() -> None:
    """Byte capture and line observation ride separate channels.

    The drain buffer keeps the child's own bytes while the line feeder decodes
    a copy to find boundaries. An undecodable payload proves the two do not
    share a buffer: a regression that decoded into the capture would show
    replacement characters here. The invalid byte sits at the end of a word so
    the decoded line stays legible without breaking the token before it.
    """
    payload = b"first\nsecond\xff\nthird"
    lines: list[str] = []

    captured = asyncio.run(
        _consume_stream(
            _reader((payload,)),
            _byte_config(io.StringIO()),
            on_line=lines.append,
        )
    )

    assert captured == payload, f"the capture must stay byte-exact, got {captured!r}"
    assert lines == ["first", "second�", "third"], (
        f"the observer must receive the decoded lines, got {lines!r}"
    )


def test_byte_exact_capture_observes_lines_across_chunk_boundaries() -> None:
    """A split multi-byte character must not corrupt either channel.

    The decoder is incremental, so a sequence spanning two chunks is held
    until complete; the capture, which never decodes, must not be affected.
    """
    chunks = (b"a\xc3", b"\xa9b\n")
    lines: list[str] = []

    captured = asyncio.run(
        _consume_stream(
            _reader(chunks),
            _byte_config(io.StringIO()),
            on_line=lines.append,
        )
    )

    assert captured == b"a\xc3\xa9b\n", (
        f"the split sequence must survive intact, got {captured!r}"
    )
    assert lines == ["aéb"], f"the joined sequence must decode once, got {lines!r}"


def test_byte_exact_unattached_stream_reports_empty_bytes() -> None:
    """An unattached byte-exact stream reports ``b""``, matching the no-lines path.

    Line observation must not change the *type* a capture-disabled or
    unattached stream reports, which is the one payload this drain does not
    render itself.
    """
    lines: list[str] = []

    captured = asyncio.run(
        _consume_stream(None, _byte_config(io.StringIO()), on_line=lines.append)
    )

    assert captured == b"", f"an empty byte capture is b'', got {captured!r}"
    assert not lines, "an unattached stream publishes no lines"
