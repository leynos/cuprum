"""Bounded output teardown remains independent of the default executor."""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import typing as typ

from cuprum._bounded_delivery import _DeliveryPayload, _DeliveryWorker
from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.events import TerminalOutcome
from cuprum.sinks import SessionOutcome, SessionStart
from cuprum.unittests._sink_test_support import BlockedSink, RecordingSink

if typ.TYPE_CHECKING:
    import collections.abc as cabc


async def _with_saturated_default_executor[T](
    operation: cabc.Awaitable[T],
) -> T:
    """Run ``operation`` while the loop's only executor worker is blocked."""
    loop = asyncio.get_running_loop()
    loop.set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=1))
    release = threading.Event()
    blocker = loop.run_in_executor(None, release.wait)
    try:
        return await asyncio.wait_for(operation, timeout=1.0)
    finally:
        release.set()
        await blocker


def test_delivery_worker_close_bypasses_a_saturated_default_executor() -> None:
    """A held executor cannot prevent worker drain grace from starting."""
    sink = BlockedSink(wait_timeout_s=5.0)
    worker = _DeliveryWorker(
        destinations={EchoStream.STDOUT: sink, EchoStream.STDERR: sink},
        max_queued_bytes=32,
    )
    assert (
        worker.enqueue(
            EchoStream.STDOUT,
            _DeliveryPayload(value="blocked", source_bytes=7),
            flush=True,
        )
        == 0
    )
    assert sink.started.wait(timeout=1)

    try:
        fallbacks = asyncio.run(
            _with_saturated_default_executor(
                _close_delivery_worker(worker, drain_grace_s=0.02),
            ),
        )
    finally:
        sink.release.set()
        assert sink.finished.wait(timeout=1)

    assert tuple(fallback.error_category for fallback in fallbacks) == (
        EchoErrorCategory.UNDELIVERED,
    )


def test_sink_session_close_bypasses_a_saturated_default_executor() -> None:
    """A held executor cannot delay bounded presentation-session close."""
    sink = RecordingSink()
    bracket = _SinkBracket.open(
        sink,
        SessionStart(label="test", argv=()),
        bounded_delivery=True,
        delivery_grace_s=0.02,
    )
    outcome = SessionOutcome(outcome=TerminalOutcome.EXIT_ZERO, exit_code=0)

    asyncio.run(
        _with_saturated_default_executor(bracket.aclose(outcome=outcome)),
    )

    assert sink.last_session.closed_event.is_set()
    assert sink.last_session.closed == 1
