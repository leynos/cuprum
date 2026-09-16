"""Direct subprocesses whose owner reaps them with POSIX ``wait4``.

``asyncio.subprocess`` delegates child reaping to a process-global watcher,
which returns an exit status but discards the child-specific resource usage.
This module owns the direct-command POSIX path so its one reaper can return
both values. Pipeline stages continue to use asyncio's watcher because their
concurrent resource figures cannot be attributed to individual stages.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import os
import subprocess  # ruff: ignore[suspicious-subprocess-import] - this isolated path owns wait4 reaping.
import typing as typ
from asyncio.streams import FlowControlMixin

from cuprum._rusage import (
    ChildResourceUsage,
    _ChildRusageSnapshot,
    capture_child_rusage,
    child_rusage_delta,
    resource_usage_from_wait4,
    wait4_resource_measurement_available,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


@dc.dataclass(frozen=True, slots=True)
class DirectProcessConfig:
    """The direct-child spawn inputs shared by asyncio and ``wait4`` paths."""

    argv: tuple[str, ...]
    stdin: int | None
    stdout: int | None
    stderr: int | None
    env: cabc.Mapping[str, str] | None
    cwd: str | None


def _close_waiter(
    close_future: asyncio.Future[None],
    _: asyncio.StreamWriter,
) -> asyncio.Future[None]:
    """Adapt the pipe-close future to asyncio's writer protocol callback."""
    return close_future


class _WritePipeProtocol(FlowControlMixin):
    """Provide ``StreamWriter`` back-pressure and close waiting for a pipe."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        """Initialize the flow-control state and close waiter."""
        super().__init__(loop)
        self._closed = loop.create_future()
        self._get_close_waiter = functools.partial(_close_waiter, self._closed)

    def connection_lost(self, exc: Exception | None) -> None:
        """Settle the close waiter after the pipe transport disconnects."""
        super().connection_lost(exc)
        if self._closed.done():
            return
        if exc is None:
            self._closed.set_result(None)
        else:
            self._closed.set_exception(exc)


class _Wait4Process(asyncio.subprocess.Process):
    """An asyncio-compatible direct child with a single owning ``wait4`` call."""

    def __init__(
        self,
        popen: subprocess.Popen[bytes],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        """Wrap a spawned child before attaching its pipes to ``loop``."""
        self._popen = popen
        # The inherited signal methods delegate to this transport. Popen offers
        # the same signalling interface, while this class keeps reaping local.
        self._transport = popen
        self._loop = loop
        self._returncode: int | None = None
        self._reap_task: asyncio.Task[int] | None = None
        self._resource_usage: ChildResourceUsage | None = None
        self._pipe_transports: list[asyncio.BaseTransport] = []
        self.pid = popen.pid
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None

    @property
    def returncode(self) -> int | None:
        """Exit code after this instance's reaper has received it."""
        return self._returncode

    @property
    def resource_usage(self) -> ChildResourceUsage | None:
        """Usage delivered by this child instance's ``wait4`` call."""
        return self._resource_usage

    async def connect_pipes(self) -> None:
        """Attach the Popen pipes to asyncio readers and writers."""
        if self._popen.stdout is not None:
            self.stdout = await self._connect_reader(self._popen.stdout)
        if self._popen.stderr is not None:
            self.stderr = await self._connect_reader(self._popen.stderr)
        if self._popen.stdin is not None:
            self.stdin = await self._connect_writer(self._popen.stdin)

    async def _connect_reader(self, pipe: typ.IO[bytes]) -> asyncio.StreamReader:
        """Connect one child output pipe to a stream reader."""
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        transport, _ = await self._loop.connect_read_pipe(lambda: protocol, pipe)
        self._pipe_transports.append(transport)
        return reader

    async def _connect_writer(self, pipe: typ.IO[bytes]) -> asyncio.StreamWriter:
        """Connect the child input pipe to a flow-controlled stream writer."""
        protocol = _WritePipeProtocol(self._loop)
        transport, _ = await self._loop.connect_write_pipe(lambda: protocol, pipe)
        self._pipe_transports.append(transport)
        return asyncio.StreamWriter(transport, protocol, None, self._loop)

    async def wait(self) -> int:
        """Await the owned reap without allowing caller cancellation to cancel it."""
        if self._reap_task is None:
            self._reap_task = asyncio.create_task(self._reap())
        return await asyncio.shield(self._reap_task)

    async def _reap(self) -> int:
        """Perform the one blocking child reap in an executor thread."""
        pid, status, usage = await asyncio.to_thread(os.wait4, self.pid, 0)
        if pid != self.pid:
            msg = f"wait4 reaped unexpected child {pid}, expected {self.pid}"
            raise RuntimeError(msg)
        self._returncode = os.waitstatus_to_exitcode(status)
        self._popen.returncode = self._returncode
        self._resource_usage = resource_usage_from_wait4(usage)
        return self._returncode


def _spawn_popen(config: DirectProcessConfig) -> subprocess.Popen[bytes]:
    """Spawn the validated command synchronously before connecting its pipes."""
    # ruff: ignore[subprocess-without-shell-equals-true] - argv validated; shell=False.
    return subprocess.Popen(
        config.argv,
        stdin=config.stdin,
        stdout=config.stdout,
        stderr=config.stderr,
        env=config.env,
        cwd=config.cwd,
        shell=False,
    )


async def spawn_wait4_process(
    config: DirectProcessConfig,
) -> asyncio.subprocess.Process:
    """Spawn a direct POSIX child that owns its ``wait4`` resource usage."""
    loop = asyncio.get_running_loop()
    process = _Wait4Process(_spawn_popen(config), loop)
    try:
        await process.connect_pipes()
    except BaseException:
        process.kill()
        await process.wait()
        raise
    return process


async def spawn_direct_process(
    config: DirectProcessConfig,
) -> asyncio.subprocess.Process:
    """Spawn a direct process using ``wait4`` where it can own the reap."""
    if wait4_resource_measurement_available():
        return await spawn_wait4_process(config)
    return await asyncio.create_subprocess_exec(
        *config.argv,
        stdin=config.stdin,
        stdout=config.stdout,
        stderr=config.stderr,
        env=config.env,
        cwd=config.cwd,
    )


def capture_resource_before_spawn() -> _ChildRusageSnapshot | None:
    """Capture aggregate fallback usage only when ``wait4`` is unavailable."""
    return None if wait4_resource_measurement_available() else capture_child_rusage()


def resource_usage_for(
    process: asyncio.subprocess.Process,
    before: _ChildRusageSnapshot | None,
) -> ChildResourceUsage | None:
    """Return owned ``wait4`` usage or the aggregate CPU-only fallback delta."""
    direct_usage = (
        process.resource_usage if isinstance(process, _Wait4Process) else None
    )
    return direct_usage or child_rusage_delta(before, capture_child_rusage())


__all__ = [
    "DirectProcessConfig",
    "capture_resource_before_spawn",
    "resource_usage_for",
    "spawn_direct_process",
]
