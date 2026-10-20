"""Line-emission properties for chunked stream drains."""

from __future__ import annotations

import asyncio
import io

import pytest
from hypothesis import HealthCheck, example, given, settings

from cuprum._streams import _consume_stream
from cuprum.unittests._stream_drain_support import (
    _PROPERTY_MAX_EXAMPLES,
    _config,
    _expected_emitted_lines,
    _line_text_and_chunks,
    _payload_and_chunks,
    _reader,
)


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

    assert (
        plain.payload == with_lines.payload == payload.decode("utf-8", errors="replace")
    ), (
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


@settings(
    max_examples=_PROPERTY_MAX_EXAMPLES,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(case=_line_text_and_chunks())
def test_line_emission_matches_splitlines_for_every_supported_boundary(
    case: tuple[str, tuple[bytes, ...]],
) -> None:
    """Property: decoded chunk splits preserve the universal-line oracle."""
    text, chunks = case
    received: list[str] = []

    asyncio.run(
        _consume_stream(
            _reader(chunks),
            _config(io.StringIO()),
            on_line=received.append,
        )
    )

    assert received == text.splitlines(), (
        "line emission must match str.splitlines() across every supported "
        f"boundary and byte partition, got {received!r} for {text!r}"
    )
