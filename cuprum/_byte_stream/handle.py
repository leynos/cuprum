"""The owned handle returned by ``SafeCmd.start_bytes()``.

``RunningBytesCommand`` is the live counterpart of ``RunningCommand``: the
child, stdin writer, supervisor, stop lock, control operations, and teardown
are the same, and the difference is what the handle exposes. Instead of a text
result assembled at settlement and no live output, it owns two independent
``ByteStreamReader`` objects and settles to a ``BytesCommandResult``.

The handle adds no second settlement path. Its settlement plan is built beside
its result assembly in :mod:`cuprum._byte_stream.settlement`; the one
settlement task, the deadline rule, and the shielded stop-then-settle teardown
come from the shared base.
"""

from __future__ import annotations

import typing as typ

# The reader is a public return annotation, so it must remain a runtime name:
# ``typing.get_type_hints`` resolves the property from this module's globals.
from cuprum._byte_stream.reader import (
    ByteStreamReader,  # ruff: ignore[typing-only-first-party-import] - public annotation resolves at runtime
)
from cuprum._byte_stream.settlement import bytes_settlement_plan
from cuprum._process_handle.base import _OwnedCommandHandle
from cuprum._process_handle.settlement import _Settlement

# The result type is a public return annotation, so it must remain a runtime
# name: ``typing.get_type_hints`` resolves ``wait`` from this module's globals.
from cuprum.sh.byte_results import (
    BytesCommandResult,  # ruff: ignore[typing-only-first-party-import] - public annotation resolves at runtime
)

if typ.TYPE_CHECKING:
    from cuprum._byte_stream.wiring import _ByteStreams
    from cuprum._process_handle.tasks import _HandleRun

__all__ = ["RunningBytesCommand"]


class RunningBytesCommand(_OwnedCommandHandle["BytesCommandResult"]):
    """An owned handle to a running command whose streams are read as bytes.

    Returned by :meth:`SafeCmd.start_bytes`. The child is already spawned and
    announced, and the handle owns its lifetime exactly as ``start()`` does:
    leaving an ``async with`` block stops and settles the child, ``wait``
    reports the outcome, and ``terminate``, ``kill``, and ``signal`` control
    it.

    The live half is split in two. ``stdout`` and ``stderr`` are independent
    :class:`~cuprum.sh.ByteStreamReader` objects: each can be iterated, can
    observe its own end, and can be closed without touching the other or the
    child. Bytes are delivered losslessly while a reader is live, so a consumer
    that stops reading applies backpressure to the child; once settlement
    begins, bytes that do not fit the queue are discarded from the reader while
    the run's capture keeps them, and the reader reports
    ``TRUNCATED_BY_SETTLEMENT``.

    Consume to end-of-stream before awaiting the result when the live stream and
    the captured result must agree. A ``wait`` that starts first is a
    settlement, and a settlement may release the queues before every remaining
    byte has been delivered live.

    A ``wait`` deadline bounds one caller only. Its expiry stops the child the
    way a timed-out ``run()`` does and raises
    :class:`~cuprum.sh.TimeoutExpired` carrying the bytes captured before the
    deadline; because the child is stopped and reaped, the outcome is recorded
    and every later wait or control operation reports it.
    """

    __slots__ = ("_streams",)

    def __init__(self, run: _HandleRun, streams: _ByteStreams) -> None:
        """Take ownership of a spawned child and its live stream bundle."""
        super().__init__(run, _Settlement(run, bytes_settlement_plan(streams)))
        self._streams = streams

    @property
    def stdout(self) -> ByteStreamReader:
        """The reader for the child's standard output.

        Returns
        -------
        ByteStreamReader
            The independent reader over the child's stdout bytes.
        """
        return self._streams.stdout

    @property
    def stderr(self) -> ByteStreamReader:
        """The reader for the child's standard error.

        Returns
        -------
        ByteStreamReader
            The independent reader over the child's stderr bytes.
        """
        return self._streams.stderr

    async def wait(
        self,
        *,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # The deadline bounds this call, and asyncio.timeout cannot serve an awaitable a handle owns.
    ) -> BytesCommandResult:
        """Wait for the command to finish and return its byte-exact outcome.

        The first caller to arrive settles the run; every later caller awaits
        the same settlement and sees the same outcome, so calling this
        repeatedly or concurrently is safe and reports one result.

        Parameters
        ----------
        timeout : float | None, default=None
            Seconds to allow the command to finish. Expiry stops the child the
            way a timed-out ``run()`` does and raises
            :class:`~cuprum.sh.TimeoutExpired` carrying the bytes captured
            before the deadline. ``None`` falls back to the deadline
            :meth:`SafeCmd.start_bytes` resolved for this handle, and to no
            deadline at all when it resolved none.

        Returns
        -------
        BytesCommandResult
            The command's outcome once it has exited.

        Raises
        ------
        TimeoutExpired
            If *timeout*, or the deadline ``start_bytes()`` resolved, elapsed
            before the command finished.
        LiveStreamError
            If a stream consumer or the supervisor failed. The failed reader
            reports ``FAILED`` and the other is truncated.
        """  # ruff: ignore[docstring-extraneous-exception] - both errors propagate from settlement
        deadline = self._run.default_timeout if timeout is None else timeout
        return await self._settlement.outcome(deadline)
