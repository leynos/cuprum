"""Drain a line stream's settled consumers once the child has exited.

Part of the ``cuprum._line_stream`` package. This module owns the single post-exit drain
step; the failure-path reconciliation it falls back to lives in
``cuprum._line_stream.coordinator`` because a test patches that module's
``_drain_stream_consumers`` name directly.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._stream_drain import _decode_consumer_drains
from cuprum._subprocess_stdin_reclaim import _settle_stdin_writer
from cuprum._subprocess_wait import _consumer_awaitable, _drain_context

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _DrainPair

__all__ = ["_drain_after_exit"]


async def _drain_after_exit(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> _DrainPair:
    """Await the settled consumers and drain them exactly once on failure.

    Returns
    -------
    _DrainPair
        The stdout and stderr drain results, in that order. Each carries the
        payload *and* the capture record and spill handle beside it, which is
        what makes this pair sufficient to build a result: the caller reads
        ``.payload`` for the text and hands the whole record on, so a spilled
        capture's file is still reachable after the drain has been consumed.
    """
    # Imported here, not at module scope, to avoid a cycle: ``_discard_drain``
    # lives in ``cuprum._line_stream.coordinator`` because a test patches its
    # module-level ``_drain_stream_consumers`` name, and that module imports
    # this one.
    from cuprum._line_stream.coordinator import _discard_drain

    if run.tasks.stdin_task is not None:
        try:
            await _settle_stdin_writer(run.tasks.stdin_task)
        except BaseException:
            await _discard_drain(run, pid, execution)
            raise
    # The pair may hold ``None`` for a stream cuprum holds no pipe for; the
    # stand-in keeps each result in its own stream's position. ``gather``
    # reports a list however many awaitables it was handed, so the pair is
    # unpacked by hand rather than returned as it comes back. A settled
    # ``None`` is then decoded to that stream's own empty capture, because
    # the caller reads ``.payload`` and ``.capture`` off both slots.
    try:
        stdout_drain, stderr_drain = _decode_consumer_drains(
            await asyncio.gather(
                *(_consumer_awaitable(task) for task in run.tasks.consumers)
            ),
            context=_drain_context(execution, run.tasks, pid, preserve=True),
        )
    except BaseException:
        await _discard_drain(run, pid, execution)
        raise
    # Settled here, on the one path that read every consumer to completion:
    # the caller assembles its result from these collectors once this returns,
    # and ``snapshot()`` reports ``()`` until a settle marks the drain done.
    # The failure paths above re-raise, so no result is assembled after them
    # and the collectors they leave unsettled are read by nobody.
    for diagnostics in run.tasks.relay_diagnostics:
        diagnostics.settle()
    return stdout_drain, stderr_drain
