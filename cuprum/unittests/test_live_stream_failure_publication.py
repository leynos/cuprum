"""A consumer drain's failure must reach its reader without settlement.

The settlement task races the consumers only from the moment a caller asks for
an outcome. A reader iterating a live stream has not asked for one, so the
drain task's own failure has to publish itself. These tests drive that one
callback: an exception publishes, cancellation does not.
"""

from __future__ import annotations

import asyncio
import functools

import pytest

from cuprum._stream_live import _StopRequest
from cuprum._stream_tap import _StreamTap
from cuprum._subprocess_streams import (
    _publish_consumer_outcome,
    _publish_writer_failure,
)


def _recording_tap(
    published: list[BaseException],
    completed: list[bool] | None = None,
) -> _StreamTap:
    """Build a tap that records published failures and completions."""
    return _StreamTap(
        on_chunk=lambda _chunk: None,
        on_eof=lambda: None,
        on_failure=published.append,
        on_complete=None if completed is None else lambda: completed.append(True),
    )


def test_a_failed_consumer_publishes_its_exception() -> None:
    """A task that raises hands its own exception to the tap."""

    async def scenario() -> None:
        """Fail a task and let its done callback run."""
        published: list[BaseException] = []
        tap = _recording_tap(published)

        async def fail() -> None:
            """Suspend once, then raise the failure under test."""
            await asyncio.sleep(0)
            message = "drain failed"
            raise RuntimeError(message)

        task = asyncio.create_task(fail())
        task.add_done_callback(functools.partial(_publish_consumer_outcome, tap))
        with pytest.raises(RuntimeError, match="drain failed"):
            await task
        await asyncio.sleep(0)
        assert len(published) == 1, f"expected one publication, got {published!r}"
        assert isinstance(published[0], RuntimeError), (
            f"the original error must be published, got {published[0]!r}"
        )

    asyncio.run(scenario())


def test_cancelled_tasks_publish_nothing() -> None:
    """Teardown cancellation publishes no failure and requests no stop."""

    async def scenario() -> None:
        """Cancel a consumer and a writer, then let their callbacks run."""
        published: list[BaseException] = []
        consumer_tap = _recording_tap(published)
        stop = _StopRequest()
        consumer = asyncio.create_task(asyncio.sleep(30))
        consumer.add_done_callback(
            functools.partial(_publish_consumer_outcome, consumer_tap)
        )
        writer = asyncio.create_task(asyncio.sleep(30))
        writer.add_done_callback(functools.partial(_publish_writer_failure, stop))
        for task in (consumer, writer):
            task.cancel()
        for task in (consumer, writer):
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.sleep(0)
        assert not published, f"cancellation must publish nothing, got {published!r}"
        assert not stop.requested, "cancellation must not request a stop"

    asyncio.run(scenario())


def test_a_successful_consumer_confirms_the_end_it_observed() -> None:
    """A task that returns normally publishes the completion it noted."""

    async def scenario() -> None:
        """Complete a task and let its done callback run."""
        completed: list[bool] = []
        tap = _recording_tap([], completed)

        async def finish() -> None:
            """Suspend once, then return normally."""
            await asyncio.sleep(0)

        task = asyncio.create_task(finish())
        task.add_done_callback(functools.partial(_publish_consumer_outcome, tap))
        await task
        await asyncio.sleep(0)
        assert completed == [True], f"expected one completion, got {completed!r}"

    asyncio.run(scenario())


def start_error_retained(stop: _StopRequest) -> bool:
    """Return whether the stop retained the failure that requested it."""
    return isinstance(stop.failure, RuntimeError)


def test_a_failed_stdin_writer_requests_a_stop() -> None:
    """A writer failure asks for the held child to be stopped."""

    async def scenario() -> None:
        """Fail a writer task and let its done callback run."""
        stop = _StopRequest()
        started: list[bool] = []

        async def request_stop() -> None:
            """Suspend once, then record that the bound stop was started."""
            await asyncio.sleep(0)
            started.append(True)

        stop.bind(request_stop)

        async def fail() -> None:
            """Suspend once, then fail the writer contract."""
            await asyncio.sleep(0)
            message = "producer failed"
            raise RuntimeError(message)

        task = asyncio.create_task(fail())
        task.add_done_callback(functools.partial(_publish_writer_failure, stop))
        with pytest.raises(RuntimeError, match="producer failed"):
            await task
        await stop.await_stop()
        assert stop.requested, "a failed writer must record a stop request"
        assert start_error_retained(stop), "the cause must be retained"
        assert started == [True], "the bound stop must be started"

    asyncio.run(scenario())
