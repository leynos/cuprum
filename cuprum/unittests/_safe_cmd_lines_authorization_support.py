"""Shared seams for the deferred-authorization ``SafeCmd.lines()`` tests.

The spawn seam is observed by patching the ``spawn`` module's
``_spawn_subprocess`` global, which is the reference ``_start_line_stream_run``
resolves, and the delivery-worker helpers watch the daemon thread a bounded echo
starts. Both belong here rather than in one test module because three modules
exercise them.
"""

from __future__ import annotations

import asyncio
import threading
import typing as typ

import pytest

from cuprum import RunOutputOptions
from cuprum.sh import BoundedDelivery, ExecutionContext

if typ.TYPE_CHECKING:
    import asyncio.subprocess as asyncio_subprocess

    from cuprum._subprocess_execution import _SubprocessExecution


class _SpawnRecorder:
    """Record every ``_spawn_subprocess`` call, delegating to the real one.

    ``executions`` answers whether a run reached the spawn and with which
    timeout; ``processes`` hands the test the children that did start.
    """

    __slots__ = ("_real_spawn", "executions", "processes")

    def __init__(self) -> None:
        """Start with no recorded calls."""
        # ``spawn`` imports the spawn by name, so patching that global replaces
        # the reference this recorder would otherwise call back into. Holding
        # the defining module's function keeps the delegate real.
        from cuprum._subprocess_spawn import _spawn_subprocess

        self._real_spawn = _spawn_subprocess
        self.executions: list[_SubprocessExecution] = []
        self.processes: list[asyncio_subprocess.Process] = []

    async def __call__(
        self,
        execution: _SubprocessExecution,
    ) -> asyncio_subprocess.Process:
        """Record the execution, then spawn exactly what was recorded.

        Returns
        -------
        asyncio.subprocess.Process
            The child the real spawn produced.
        """
        self.executions.append(execution)
        process = await self._real_spawn(execution)
        self.processes.append(process)
        return process


@pytest.fixture
def spawn_recorder(monkeypatch: pytest.MonkeyPatch) -> _SpawnRecorder:
    """Patch the ``spawn`` module's spawn seam with a recording delegate."""
    from cuprum._line_stream import spawn

    recorder = _SpawnRecorder()
    monkeypatch.setattr(spawn, "_spawn_subprocess", recorder)
    return recorder


def _delivery_thread_is_alive() -> bool:
    """Report whether a bounded-delivery worker thread is still running."""
    return any(
        thread.name == "cuprum-output-delivery" and thread.is_alive()
        for thread in threading.enumerate()
    )


async def _wait_for_delivery_threads_to_stop(deadline_s: float = 2.0) -> bool:
    """Give a closing delivery worker a bounded moment to exit its thread.

    Returns
    -------
    bool
        ``True`` once no delivery-worker thread is alive, ``False`` if one
        outlives the deadline. The wait only observes a worker that was
        already asked to stop; it never lets a run make progress.
    """
    loop = asyncio.get_running_loop()
    loop_deadline = loop.time() + deadline_s
    while _delivery_thread_is_alive():
        if loop.time() > loop_deadline:
            return False
        # A daemon thread cannot be awaited; yield so it can make progress.
        await asyncio.sleep(0.01)
    return True


def _line_stream_delivery(
    sink: typ.IO[str],
) -> tuple[RunOutputOptions, ExecutionContext]:
    """Return options and context whose echo uses a bounded delivery worker.

    Returns
    -------
    tuple[RunOutputOptions, ExecutionContext]
        Options that echo stdout through a bounded delivery worker, and the
        context naming the sink it drains into.
    """
    return (
        RunOutputOptions(
            echo_stdout=True,
            echo_stderr=False,
            delivery=BoundedDelivery(max_queued_bytes=128, drain_grace_s=0.05),
        ),
        ExecutionContext(stdout_sink=sink),
    )
