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

from cuprum._stream_tap import _StreamTap
from cuprum._subprocess_streams import _publish_consumer_failure


def _recording_tap(published: list[BaseException]) -> _StreamTap:
    """Build a tap that records every published failure."""
    return _StreamTap(
        on_chunk=lambda _chunk: None,
        on_eof=lambda: None,
        on_failure=published.append,
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
        task.add_done_callback(functools.partial(_publish_consumer_failure, tap))
        with pytest.raises(RuntimeError, match="drain failed"):
            await task
        await asyncio.sleep(0)
        assert len(published) == 1, f"expected one publication, got {published!r}"
        assert isinstance(published[0], RuntimeError), (
            f"the original error must be published, got {published[0]!r}"
        )

    asyncio.run(scenario())


def test_a_cancelled_consumer_publishes_nothing() -> None:
    """Teardown cancellation is not a failed stream."""

    async def scenario() -> None:
        """Cancel a task and let its done callback run."""
        published: list[BaseException] = []
        tap = _recording_tap(published)
        task = asyncio.create_task(asyncio.sleep(30))
        task.add_done_callback(functools.partial(_publish_consumer_failure, tap))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert not published, f"cancellation must publish nothing, got {published!r}"

    asyncio.run(scenario())
