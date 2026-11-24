"""Reader-release contract for the inter-stage pump's early-close path.

After a downstream close, the pump drains the upstream reader for a bounded
window. These tests pin what happens when that window expires: the reader's
transport is closed exactly once so a still-writing producer fails on a broken
pipe instead of blocking on a pipe nobody reads, while a drain that reaches EOF
or a transport that is already closing is left untouched.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum._streams import _POST_CLOSE_DRAIN_TIMEOUT_S, _pump_stream
from cuprum._streams_pump import _relay_chunks
from cuprum.stream_events import StreamOperationOutcome
from cuprum.unittests._stream_pump_support import (
    _RecordingTransport,
    _StallingPumpReader,
    _StubPumpReader,
    _StubPumpWriter,
)

# The outer bound each exercise runs under. A release returns promptly; a pump
# that tried to drain the stalling reader to EOF would never return.
_EXERCISE_TIMEOUT_S = _POST_CLOSE_DRAIN_TIMEOUT_S * 8


def _stalling_reader(transport: _RecordingTransport) -> asyncio.StreamReader:
    """Return a stalling reader double carrying *transport*."""
    reader = _StallingPumpReader(b"first", transport=transport)
    return typ.cast("asyncio.StreamReader", reader)


def _closed_writer() -> asyncio.StreamWriter:
    """Return a writer double that reports downstream closure on first drain."""
    return typ.cast("asyncio.StreamWriter", _StubPumpWriter(fail_on_drain_call=1))


def test_post_close_drain_timeout_releases_upstream_reader() -> None:
    """A timed-out post-close drain closes the upstream reader exactly once."""

    async def exercise() -> tuple[StreamOperationOutcome, _RecordingTransport]:
        """Relay into a closed downstream and report the outcome and transport."""
        transport = _RecordingTransport()
        outcome = await _relay_chunks(_stalling_reader(transport), _closed_writer())
        return outcome, transport

    outcome, transport = asyncio.run(
        asyncio.wait_for(exercise(), timeout=_EXERCISE_TIMEOUT_S)
    )

    assert outcome is StreamOperationOutcome.POST_CLOSE_DRAIN_TIMEOUT, (
        f"a timed-out drain must keep reporting its own outcome, got {outcome}"
    )
    assert transport.close_calls == 1, (
        "the upstream reader must be released exactly once, got "
        f"{transport.close_calls} close calls"
    )


def test_post_close_drain_to_eof_keeps_reader_open() -> None:
    """A bounded drain that reaches EOF leaves the reader to asyncio."""

    async def exercise() -> tuple[StreamOperationOutcome, _RecordingTransport]:
        """Relay a finite upstream through a writer that closes immediately."""
        transport = _RecordingTransport()
        reader = typ.cast(
            "asyncio.StreamReader",
            _StubPumpReader([b"a", b"b"], transport=transport),
        )
        outcome = await _relay_chunks(reader, _closed_writer())
        return outcome, transport

    outcome, transport = asyncio.run(
        asyncio.wait_for(exercise(), timeout=_EXERCISE_TIMEOUT_S)
    )

    assert outcome is StreamOperationOutcome.DOWNSTREAM_CLOSED, (
        f"a completed drain must report downstream closure, got {outcome}"
    )
    assert transport.close_calls == 0, (
        "a reader drained to EOF must not be closed, got "
        f"{transport.close_calls} close calls"
    )


def test_release_skips_a_reader_transport_already_closing() -> None:
    """An already-closing transport is not closed again."""

    async def exercise() -> _RecordingTransport:
        """Relay from a reader whose transport is already closing."""
        transport = _RecordingTransport(is_closing=True)
        outcome = await _relay_chunks(_stalling_reader(transport), _closed_writer())
        assert outcome is StreamOperationOutcome.POST_CLOSE_DRAIN_TIMEOUT, (
            f"the drain must still time out, got {outcome}"
        )
        return transport

    transport = asyncio.run(asyncio.wait_for(exercise(), timeout=_EXERCISE_TIMEOUT_S))

    assert transport.close_calls == 0, (
        "an already-closing transport must be left alone, got "
        f"{transport.close_calls} close calls"
    )


@pytest.mark.parametrize("error", [OSError("already closed"), RuntimeError("loop")])
def test_release_suppresses_transport_close_errors(
    error: OSError | RuntimeError,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failing transport close is logged and cannot displace writer teardown."""
    transport = _RecordingTransport(error=error)
    writer = _StubPumpWriter(fail_on_drain_call=1)

    async def exercise() -> None:
        """Pump through a failing reader transport and a closed writer."""
        await _pump_stream(
            _stalling_reader(transport),
            typ.cast("asyncio.StreamWriter", writer),
        )

    with caplog.at_level(logging.DEBUG, logger="cuprum._streams_pump"):
        asyncio.run(asyncio.wait_for(exercise(), timeout=_EXERCISE_TIMEOUT_S))

    assert transport.close_calls == 1, (
        f"the release must still attempt the close once, got {transport.close_calls}"
    )
    assert writer.closed is True, "a suppressed reader error must not skip writer close"
    assert writer.wait_closed_calls == 1, "writer closure must still be awaited once"
    suppressed = [
        record
        for record in caplog.records
        if "stream_reader_release_suppressed" in record.message
    ]
    assert suppressed, "a suppressed reader close must be reported at DEBUG"


def test_release_tolerates_a_reader_without_transport() -> None:
    """A reader double with no transport completes without error."""

    async def exercise() -> StreamOperationOutcome:
        """Relay a stalling reader that exposes no transport at all."""
        reader = typ.cast("asyncio.StreamReader", _StallingPumpReader(b"first"))
        return await _relay_chunks(reader, _closed_writer())

    outcome = asyncio.run(asyncio.wait_for(exercise(), timeout=_EXERCISE_TIMEOUT_S))

    assert outcome is StreamOperationOutcome.POST_CLOSE_DRAIN_TIMEOUT, (
        f"a transport-less reader must still report the timeout, got {outcome}"
    )
