"""Runtime coverage for pipeline stream-pump behaviour."""

from __future__ import annotations

import asyncio
import logging
import textwrap
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import ScopeConfig, scoped, sh
from cuprum._streams import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _drain_stream_reader_bounded,
    _pump_stream,
)
from tests.helpers.catalogue import python_catalogue


class _StubPumpReader:
    """Stub stream reader yielding queued chunks then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        """Initialize the stub with queued chunks."""
        self._chunks = list(chunks)
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return the next queued chunk, or empty bytes at EOF."""
        self.read_calls += 1
        await asyncio.sleep(0)
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _NeverEndingPumpReader:
    """Stub stream reader that emits once and never reaches EOF."""

    def __init__(self) -> None:
        """Initialize the reader."""
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return one chunk, then wait forever unless the pump cancels the read."""
        self.read_calls += 1
        if self.read_calls == 1:
            return b"first"
        await asyncio.sleep(3600)
        return b"more"


class _PartiallyDrainedPumpReader:
    """Stub reader that yields discarded bytes before stalling."""

    def __init__(self) -> None:
        """Initialize the reader."""
        self.read_calls = 0

    async def read(self, _: int) -> bytes:
        """Return one discarded chunk, then wait until cancelled."""
        self.read_calls += 1
        if self.read_calls == 1:
            return b"discarded"
        await asyncio.sleep(3600)
        return b"more"


class _StubPumpWriter:
    """Stub stream writer recording writes, drains, and closure."""

    def __init__(self, *, fail_on_drain_call: int | None = None) -> None:
        """Initialize the stub with an optional drain failure point."""
        self.drain_calls = 0
        self.closed = False
        self.wait_closed_calls = 0
        self._fail_on_drain_call = fail_on_drain_call

    def write(self, _chunk: bytes) -> None:
        """Accept a chunk like ``asyncio.StreamWriter.write``."""

    async def drain(self) -> None:
        """Record drain and optionally simulate downstream closure."""
        self.drain_calls += 1
        await asyncio.sleep(0)
        if self.drain_calls == self._fail_on_drain_call:
            raise BrokenPipeError

    def write_eof(self) -> None:
        """Accept EOF signalling like ``asyncio.StreamWriter.write_eof``."""

    def close(self) -> None:
        """Mark the writer as closed."""
        self.closed = True

    async def wait_closed(self) -> None:
        """Record that closure was awaited."""
        self.wait_closed_calls += 1


@pytest.mark.usefixtures("stream_backend")
def test_pipeline_drains_upstream_after_downstream_closes() -> None:
    """Pipeline subprocess I/O completes when a downstream stage exits early."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python(
        "-c",
        textwrap.dedent(
            """
            import sys

            for _ in range(4096):
                sys.stdout.write('payload-line\\n')
                sys.stdout.flush()
            """
        ).strip(),
    )
    early_consumer = python(
        "-c",
        "import sys; sys.stdin.buffer.read(1); sys.stdout.write('done')",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | early_consumer).run_sync(timeout=5.0)

    assert result.stdout == "done", "final stage should report completion"
    assert result.stages[1].exit_code == 0, "early consumer should exit cleanly"


def test_pump_stream_logs_discarded_bytes_after_downstream_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Early downstream closure logs how many upstream bytes were discarded."""

    async def exercise() -> None:
        """Pump through a writer that fails mid-stream."""
        reader = _StubPumpReader([b"a" * 8, b"b" * 8, b"c"])
        writer = _StubPumpWriter(fail_on_drain_call=2)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )

    with caplog.at_level(logging.DEBUG, logger="cuprum.streams"):
        asyncio.run(exercise())

    discarded_records = [
        record
        for record in caplog.records
        if record.name == "cuprum.streams"
        and "stream_downstream_closed" in record.message
    ]
    assert discarded_records, "early downstream close should log discarded bytes"
    last_discard = discarded_records[-1]
    assert vars(last_discard)["cuprum_discarded_bytes"] == 1, (
        "early-close telemetry should report bytes discarded by bounded draining"
    )


def test_pump_stream_omits_downstream_close_log_without_writer(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing writers drain quietly without reporting downstream closure."""

    async def exercise() -> _StubPumpReader:
        """Pump with no writer so the reader is simply drained."""
        reader = _StubPumpReader([b"a" * 8, b"b" * 8])
        await _pump_stream(typ.cast("asyncio.StreamReader", reader), None)
        return reader

    with caplog.at_level(logging.DEBUG, logger="cuprum.streams"):
        reader = asyncio.run(exercise())

    close_records = [
        record
        for record in caplog.records
        if record.name == "cuprum.streams"
        and "stream_downstream_closed" in record.message
    ]
    assert reader.read_calls == 3, "pump should drain all chunks plus EOF"
    assert not close_records, "missing writer should not log downstream closure"


def test_pump_stream_does_not_hang_when_upstream_never_ends() -> None:
    """Early downstream closure performs bounded draining only."""

    async def exercise() -> _NeverEndingPumpReader:
        """Pump from a reader that never produces EOF."""
        reader = _NeverEndingPumpReader()
        writer = _StubPumpWriter(fail_on_drain_call=1)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader

    reader = asyncio.run(
        asyncio.wait_for(exercise(), timeout=_POST_CLOSE_DRAIN_TIMEOUT_S * 4)
    )

    assert reader.read_calls == 2, "pump should cancel the bounded drain read"


def test_bounded_drain_returns_partial_count_on_timeout() -> None:
    """A timed-out bounded drain reports bytes consumed before cancellation."""

    async def exercise() -> tuple[_PartiallyDrainedPumpReader, int]:
        """Drain one chunk from a reader that never reaches EOF."""
        reader = _PartiallyDrainedPumpReader()
        discarded_bytes = await _drain_stream_reader_bounded(
            typ.cast("asyncio.StreamReader", reader)
        )
        return reader, discarded_bytes

    reader, discarded_bytes = asyncio.run(exercise())

    assert discarded_bytes == len(b"discarded"), (
        "bounded drain should preserve bytes consumed before timeout"
    )
    assert reader.read_calls == 2, "bounded drain should cancel the stalled read"


@settings(deadline=None)
@given(
    chunks=st.lists(st.binary(min_size=1, max_size=64), min_size=0, max_size=8),
    fail_on_drain_call=st.integers(min_value=1, max_value=10),
)
def test_pump_stream_drains_generated_chunks_after_downstream_close(
    chunks: list[bytes],
    fail_on_drain_call: int,
) -> None:
    """Generated chunk streams are drained to EOF after downstream closure."""

    async def exercise() -> tuple[_StubPumpReader, _StubPumpWriter]:
        """Pump generated chunks through a writer with generated failure point."""
        reader = _StubPumpReader(chunks)
        writer = _StubPumpWriter(fail_on_drain_call=fail_on_drain_call)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader, writer

    reader, writer = asyncio.run(exercise())

    assert reader.read_calls == len(chunks) + 1, (
        "pump should always read every generated chunk plus EOF"
    )
    assert writer.closed is True, "pump should close the writer after draining"
    assert writer.wait_closed_calls == 1, "pump should await writer closure once"
