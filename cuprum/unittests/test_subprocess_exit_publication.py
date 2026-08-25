"""Regression coverage for subprocess return-code publication."""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._subprocess_execution import _wait_for_exit_code
from cuprum.sh import ExecutionContext


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


def test_wait_for_exit_code_accepts_published_returncode() -> None:
    """A published return code wins over an orphan pending wait future."""

    async def run_case() -> None:
        """Wait for the stranded process double without hanging."""
        process = typ.cast("asyncio.subprocess.Process", _PublishedExitProcess())

        exit_code, _ = await asyncio.wait_for(
            _wait_for_exit_code(process, ExecutionContext()),
            timeout=0.5,
        )

        assert exit_code == 0, f"published exit code must be returned, got {exit_code}"

    asyncio.run(run_case())
