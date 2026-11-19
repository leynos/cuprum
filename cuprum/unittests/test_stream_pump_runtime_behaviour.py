"""Runtime coverage for pipeline stream-pump behaviour."""

from __future__ import annotations

import asyncio
import logging
import textwrap
import time
import typing as typ

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cuprum import ScopeConfig, scoped, sh
from cuprum._backend import StreamBackend, get_stream_backend
from cuprum._streams import (
    _POST_CLOSE_DRAIN_TIMEOUT_S,
    _READ_SIZE,
    _drain_stream_reader_bounded,
    _pump_stream,
)
from cuprum.unittests._stream_pump_support import (
    _StallingPumpReader,
    _StubPumpReader,
    _StubPumpWriter,
)
from tests.helpers.catalogue import early_close_pipeline, python_catalogue

# The deadline the release regression allows the pipeline. A stage that is
# released settles in well under a second, so half of this bound still fails a
# regression that waits for the producer instead of releasing its reader.
_EARLY_CLOSE_DEADLINE_S = 10.0


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


def test_pipeline_settles_when_producer_outlives_early_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A producer that keeps writing cannot hold the pipeline open.

    The producer never ends on its own, so the pipeline can only settle if the
    pump releases the upstream reader once its bounded post-close drain times
    out. The producer then fails on its own broken pipe and the wait for stage
    exits completes; without the release it blocks on a full pipe forever.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", StreamBackend.PYTHON.value)
    pipeline, allowlist = early_close_pipeline()
    assert get_stream_backend() is StreamBackend.PYTHON, (
        "the release regression must run on the Python pump, not a native route"
    )

    started = time.monotonic()
    with scoped(ScopeConfig(allowlist=allowlist)):
        result = pipeline.run_sync(timeout=_EARLY_CLOSE_DEADLINE_S)
    elapsed = time.monotonic() - started

    assert result.stdout == "done", (
        f"the early consumer should still report completion, got {result.stdout!r}"
    )
    assert result.stages[1].exit_code == 0, (
        f"the early consumer should exit cleanly, got {result.stages[1].exit_code}"
    )
    assert result.stages[0].exit_code not in {None, 0}, (
        "the released producer should have exited with its own failure code, got "
        f"{result.stages[0].exit_code}"
    )
    assert elapsed < _EARLY_CLOSE_DEADLINE_S / 2, (
        "a released producer must not consume the pipeline deadline: "
        f"took {elapsed:.2f}s of {_EARLY_CLOSE_DEADLINE_S}s"
    )


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

    with caplog.at_level(logging.DEBUG, logger="cuprum._streams_pump"):
        asyncio.run(exercise())

    discarded_records = [
        record
        for record in caplog.records
        if record.name == "cuprum._streams_pump"
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

    with caplog.at_level(logging.DEBUG, logger="cuprum._streams_pump"):
        reader = asyncio.run(exercise())

    close_records = [
        record
        for record in caplog.records
        if record.name == "cuprum._streams_pump"
        and "stream_downstream_closed" in record.message
    ]
    assert reader.read_calls == 3, "pump should drain all chunks plus EOF"
    assert not close_records, "missing writer should not log downstream closure"


def test_pump_stream_forwards_explicit_read_size_to_every_reader_call() -> None:
    """The pipeline pump retains the injected benchmark read size."""

    async def exercise() -> tuple[_StubPumpReader, _StubPumpWriter]:
        """Pump through recording stream endpoints."""
        reader = _StubPumpReader([b"first", b"second"])
        writer = _StubPumpWriter()
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
            read_size=17,
        )
        return reader, writer

    reader, writer = asyncio.run(exercise())

    assert reader.read_sizes == [17, 17, 17], (
        "every pump reader call must retain the explicit read size, got "
        f"{reader.read_sizes}"
    )
    assert bytes(writer.data) == b"firstsecond", (
        f"the writer must receive the complete payload, got {writer.data!r}"
    )


def test_pump_stream_drains_slow_reader_until_eof_without_writer() -> None:
    """A writerless pump drains a slow reader to EOF, not just a bounded window."""

    async def exercise() -> _StubPumpReader:
        """Pump with no writer from a reader slower than the bounded-drain window."""
        reader = _StubPumpReader(
            [b"a" * 8, b"b" * 8, b"c" * 8],
            delay_s=_POST_CLOSE_DRAIN_TIMEOUT_S * 0.6,
        )
        await _pump_stream(typ.cast("asyncio.StreamReader", reader), None)
        return reader

    reader = asyncio.run(
        asyncio.wait_for(exercise(), timeout=_POST_CLOSE_DRAIN_TIMEOUT_S * 20)
    )

    assert reader.read_calls == 4, (
        "writerless pump should drain every chunk plus EOF rather than stopping "
        "at the bounded-drain timeout"
    )


def test_pump_stream_does_not_hang_when_upstream_never_ends() -> None:
    """Early downstream closure performs bounded draining only."""

    async def exercise() -> _StallingPumpReader:
        """Pump from a reader that never produces EOF."""
        reader = _StallingPumpReader(b"first")
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

    async def exercise() -> tuple[_StallingPumpReader, int]:
        """Drain one chunk from a reader that never reaches EOF."""
        reader = _StallingPumpReader(b"discarded")
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


def test_pump_stream_drains_per_chunk() -> None:
    """Streaming between stages awaits drain for backpressure."""

    async def exercise() -> _StubPumpWriter:
        """Drive the pump under test and capture the resulting writer."""
        reader = asyncio.StreamReader()
        writer = _StubPumpWriter()
        task = asyncio.create_task(
            _pump_stream(reader, typ.cast("asyncio.StreamWriter", writer)),
        )
        payload = b"x" * (_READ_SIZE * 2 + 1)
        reader.feed_data(payload)
        reader.feed_eof()
        await task
        return writer

    writer = asyncio.run(exercise())

    assert bytes(writer.data) == b"x" * (_READ_SIZE * 2 + 1), (
        "the writer must receive the whole payload intact"
    )
    assert writer.write_calls == writer.drain_calls, (
        "every write must be followed by a drain for backpressure"
    )
    assert writer.drain_calls >= 2, (
        "a payload spanning multiple reads must drain more than once"
    )
    assert writer.write_eof_calls == 1, "EOF must be signalled exactly once"
    assert writer.closed is True, "the pump must close the writer when done"
    assert writer.wait_closed_calls == 1, "closure must be awaited exactly once"


def test_pump_stream_handles_downstream_close_without_hanging() -> None:
    """_pump_stream drains stdout even if downstream closes mid-stream."""

    async def exercise() -> tuple[_StubPumpReader, _StubPumpWriter]:
        """Pump through a writer that fails mid-stream and return both ends."""
        reader = _StubPumpReader([b"a" * _READ_SIZE, b"b" * _READ_SIZE, b"c"])
        writer = _StubPumpWriter(fail_on_drain_call=2)
        await _pump_stream(
            typ.cast("asyncio.StreamReader", reader),
            typ.cast("asyncio.StreamWriter", writer),
        )
        return reader, writer

    reader, writer = asyncio.run(
        asyncio.wait_for(exercise(), timeout=_POST_CLOSE_DRAIN_TIMEOUT_S * 4)
    )

    assert reader.read_calls == 4, "the pump reads 3 chunks plus EOF"
    assert writer.write_calls == 2, (
        "drain fails on the second write, so the third chunk is never written"
    )
    assert writer.closed is True, (
        "the pump must close the writer even after a mid-stream failure"
    )
    assert writer.wait_closed_calls == 1, "closure must be awaited exactly once"
