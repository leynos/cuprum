"""Direct subprocesses whose owner reaps them with POSIX ``wait4``.

``asyncio.subprocess`` delegates child reaping to a process-global watcher,
which returns an exit status but discards the child-specific resource usage.
This module owns the direct-command POSIX path so its one reaper can return
both values. Pipeline stages continue to use asyncio's watcher because their
concurrent resource figures cannot be attributed to individual stages.
"""

from __future__ import annotations

import asyncio
import functools
import os
import signal
import threading
import time
import typing as typ
from asyncio.streams import FlowControlMixin

from cuprum._process_exit import (
    _NON_REAPING_PROBE_AVAILABLE,
    _await_child_waitable,
    _child_was_collected,
)
from cuprum._rusage import (
    ChildResourceUsage,
    _Wait4Usage,
    resource_usage_from_wait4,
)
from cuprum._wait4_process_errors import (
    _DeliveryRefusedError,
    _Wait4InvariantError,
)

if typ.TYPE_CHECKING:
    import subprocess


#: Backoff for the fallback reap, whose wait becomes a poll because collecting
#: is the only way such a platform can observe the child. The cap is set far
#: lower than the exit observer's, and deliberately: that observer only
#: recovers a lost wake-up, since it races the real waiter, whereas this poll
#: *is* the exit detection on such a platform. The cap therefore bounds how
#: late an exit is noticed, and so how much it inflates ``exited_at``,
#: ``duration``, and the latency a caller sees. Each attempt is one
#: non-blocking syscall made off the event loop, so a tight cap is cheap.
_REAP_POLL_INITIAL_INTERVAL = 0.01
_REAP_POLL_MAX_INTERVAL = 0.05


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

    @typ.override
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
        # Serializes the collection-and-publication against signal delivery,
        # and nothing else. It is held only from a confirmed zombie to the
        # recorded status, never across the child's lifetime, so a delivery to
        # a healthy child is never made to wait on a reap.
        self._settlement_lock = threading.Lock()
        self.pid = popen.pid
        self.stdin: asyncio.StreamWriter | None = None
        self.stdout: asyncio.StreamReader | None = None
        self.stderr: asyncio.StreamReader | None = None

    @property
    @typ.override
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

    @typ.override
    async def wait(self) -> int:
        """Await the owned reap without allowing caller cancellation to cancel it."""
        if self._reap_task is None:
            self._reap_task = asyncio.create_task(self._reap())
        return await asyncio.shield(self._reap_task)

    @typ.override
    def send_signal(self, signal: int) -> None:
        """Signal the child directly, leaving reaping to this owner alone.

        ``Popen.send_signal`` polls before signalling, and that poll is a
        ``waitpid``: it claims a zombie, after which this class's own
        ``os.wait4`` fails and the child's resource usage is lost. Sending
        directly keeps :meth:`_reap` the only reaper.

        The guard, the refusal, and the kill are taken together under
        :attr:`_settlement_lock`, so the window in which ``wait4`` has
        collected a status that ``returncode`` has not yet recorded is
        unreachable rather than merely narrow. :meth:`_delivery_is_refused`
        states what the guard asks.

        ``ProcessLookupError`` is deliberately **not** suppressed, unlike
        ``Popen.send_signal`` (bpo-40550). It reports to a handle that must
        tell its caller *how* the child ended, and swallowing the error would
        make that handle report a successful delivery to a process that no
        longer exists. The absorption belongs to the caller that can weigh it:
        ``_process_handle.control._deliver_signal`` catches it and re-reads the
        child's status.

        Raises
        ------
        _DeliveryRefusedError
            If this owner has already published the child's exit code, or if
            the kernel reports no process at its PID.
        """
        with self._settlement_lock:
            if self._delivery_is_refused():
                raise _DeliveryRefusedError(self.pid)
            os.kill(self.pid, signal)

    def _delivery_is_refused(self) -> bool:
        """Report whether a delivery is refused, collecting a status to tell.

        The caller holds :attr:`_settlement_lock`, which is what lets the
        probe-less route collect: the collection cannot block, and a delivery
        arriving while it runs waits for the publication instead of racing it.
        A child that has exited is collected there because on that route the
        collection is the only reading that can report how it ended, and the
        reading happens once: the code is published before the lock is
        released, so every later delivery refuses on the first question.

        Returns
        -------
        bool
            ``True`` when the delivery must be refused: the exit code is
            already published, or the kernel reports no waitable child at this
            owner's PID. ``False`` when a live child remains reachable.
        """
        if self._returncode is not None:
            return True
        if _NON_REAPING_PROBE_AVAILABLE:
            return _child_was_collected(self.pid)
        try:
            return self._collect_when_waitable() is not None
        except ChildProcessError:
            # No waitable child of this process remains at the pid. That is
            # the same state a collection by this owner leaves behind, and it
            # is reported as a refusal rather than allowed out as itself: the
            # delivery's contract is a ProcessLookupError, which this is not.
            return True

    @typ.override
    def terminate(self) -> None:
        """Ask the child to exit without reaping it."""
        self.send_signal(signal.SIGTERM)

    @typ.override
    def kill(self) -> None:
        """Compel the child to exit without reaping it."""
        self.send_signal(signal.SIGKILL)

    def _publish(self, status: int, usage: _Wait4Usage) -> int:
        """Record a collected status and its usage, returning the exit code."""
        code = os.waitstatus_to_exitcode(status)
        self._returncode = code
        self._popen.returncode = code
        self._resource_usage = resource_usage_from_wait4(usage)
        return code

    def _collect_and_publish(self) -> int:
        """Collect the child's status, check the owner, and record it."""
        # The caller guarantees the child is waitable, so this does not block.
        # The check runs before any assignment, so nothing of a stranger's is
        # attributed to this command.
        pid, status, usage = os.wait4(self.pid, 0)
        if pid != self.pid:
            raise _Wait4InvariantError(self.pid, pid)
        return self._publish(status, usage)

    def _collect_when_waitable(self) -> int | None:
        """Collect the child if it is waitable, collecting nothing otherwise."""
        # WNOHANG is what makes this safe under _settlement_lock: a collection
        # that could block would hold the lock for the child's whole lifetime.
        pid, status, usage = os.wait4(self.pid, os.WNOHANG)
        if pid == 0:
            return None
        if pid != self.pid:
            raise _Wait4InvariantError(self.pid, pid)
        return self._publish(status, usage)

    def _reap_without_a_probe(self) -> int:
        """Collect under the lock, polling without ever waiting under it."""
        # The lock is released between attempts, so it never spans the child's
        # lifetime. That is safe: an uncollected child is running or a zombie,
        # and neither has its PID reused — only the collection frees it, and
        # the collection publishes before the lock is released.
        interval = _REAP_POLL_INITIAL_INTERVAL
        while True:
            with self._settlement_lock:
                # The published code is read under the lock, and before the
                # collection rather than after it. A delivery's guard collects
                # on this route too, so a child it already reaped would be
                # collected a second time here — which the kernel refuses,
                # since no waitable child survives the first collection. Both
                # publish under this lock, so seeing no code means none exists
                # and the poll is free to take one of its own.
                code = self._returncode
                if code is None:
                    code = self._collect_when_waitable()
                if code is not None:
                    return code
            time.sleep(interval)
            interval = min(interval * 2, _REAP_POLL_MAX_INTERVAL)

    def _reap_blocking(self) -> int:
        """Wait for the child, then collect and publish it under the lock."""
        # The wait and the collection sit on opposite sides of the lock, so
        # the locked section is entered only once wait4 has nothing left to
        # wait for. Recording stays in the reaping thread: an event-loop hop
        # between collection and publication would reopen the window the lock
        # exists to close, and a lock cannot cover a gap across a thread
        # boundary. Where the platform cannot say whether the child is
        # waitable, the collection moves inside the lock and stops blocking,
        # rather than the lock being abandoned there. That route's delivery
        # guard collects too, and both callers take the lock first, so the two
        # can never collect the same child: whichever arrives second finds the
        # code already published.
        if _await_child_waitable(self.pid):
            with self._settlement_lock:
                return self._collect_and_publish()
        return self._reap_without_a_probe()

    async def _reap(self) -> int:
        """Perform the one blocking child reap in an executor thread."""
        return await asyncio.to_thread(self._reap_blocking)
