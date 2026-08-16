"""Wait for a subprocess exit without trusting a stranded wait future."""

from __future__ import annotations

import asyncio

_PROCESS_EXIT_POLL_INTERVAL = 0.01


async def _await_process_exit(process: asyncio.subprocess.Process) -> int:
    """Return an exit code from either the waiter or its published value."""
    if process.returncode is not None:
        return process.returncode

    async def _published_returncode() -> int:
        """Poll the authoritative return code at a low frequency."""
        # ASYNC110: Process exposes no completion event, so its published
        # return code is the only non-blocking recovery signal for a lost wake-up.
        while process.returncode is None:  # noqa: ASYNC110
            await asyncio.sleep(_PROCESS_EXIT_POLL_INTERVAL)
        return process.returncode

    wait_task = asyncio.create_task(process.wait())
    published_task = asyncio.create_task(_published_returncode())
    try:
        completed, _ = await asyncio.wait(
            (wait_task, published_task),
            return_when=asyncio.FIRST_COMPLETED,
        )
        return wait_task.result() if wait_task in completed else published_task.result()
    finally:
        for task in (wait_task, published_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(wait_task, published_task, return_exceptions=True)
