"""Unit tests for subprocess timeout cleanup."""

from __future__ import annotations

import asyncio

import pytest

from cuprum._subprocess_timeout import (
    _handle_stream_timeout,
    _SubprocessTimeoutError,
)


def test_stream_timeout_preserves_timeout_when_consumer_fails() -> None:
    """A stream-consumer failure cannot mask a timeout during cleanup."""

    async def block_stdin() -> None:
        """Remain pending until timeout cleanup cancels the task."""
        await asyncio.Event().wait()

    async def fail_consumer() -> str | None:
        """Yield once, then raise the stream-consumer failure."""
        await asyncio.sleep(0)
        msg = "consumer failed"
        raise RuntimeError(msg)

    async def handle_timeout() -> None:
        """Run the timeout handler and assert its cleanup outcome."""
        stdin_task = asyncio.create_task(block_stdin())
        consumers = (
            asyncio.create_task(fail_consumer()),
            asyncio.create_task(asyncio.sleep(0, result="stderr")),
        )

        with pytest.raises(_SubprocessTimeoutError) as exc_info:
            await _handle_stream_timeout(
                TimeoutError(),
                stdin_task=stdin_task,
                consumers=consumers,
                timeout=1.0,
            )

        assert stdin_task.cancelled()
        assert exc_info.value.stdout is None
        assert exc_info.value.stderr == "stderr"

    asyncio.run(handle_timeout())
