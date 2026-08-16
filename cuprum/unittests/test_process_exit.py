"""Regression coverage for resilient subprocess exit waiting."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._process_exit import _await_process_exit


class _PublishedExitProcess:
    """Process double whose waiter strands after publishing an exit code."""

    def __init__(self) -> None:
        """Start without a published return code."""
        self.returncode: int | None = None

    async def wait(self) -> int:
        """Publish success, then model asyncio's orphan pending wait future."""
        for _ in range(3):
            await asyncio.sleep(0)
        self.returncode = 0
        await asyncio.Event().wait()
        return 0


def test_await_process_exit_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending waiter."""

    async def run_case() -> int:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())
        return await asyncio.wait_for(_await_process_exit(process), timeout=0.5)

    assert asyncio.run(run_case()) == 0, (
        "a published exit code must complete a stranded process waiter"
    )
