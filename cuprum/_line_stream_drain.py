"""Drain a line stream's settled consumers once the child has exited.

Split out of ``cuprum._line_stream`` to keep that module within the
repository's line-count limit. This module owns the single post-exit drain
step; the failure-path reconciliation it falls back to stays in
``cuprum._line_stream`` because a test patches that module's
``_drain_stream_consumers`` name directly.
"""

from __future__ import annotations

import asyncio
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._line_stream_queue import _LineStreamRun
    from cuprum._subprocess_execution import _SubprocessExecution

__all__ = ["_drain_after_exit"]


async def _drain_after_exit(
    run: _LineStreamRun,
    pid: int | None,
    execution: _SubprocessExecution,
) -> tuple[str | None, str | None]:
    """Await the settled consumers and drain them exactly once on failure."""
    # Imported here, not at module scope, to avoid a cycle: ``_discard_drain``
    # stays in ``cuprum._line_stream`` because a test patches its module-level
    # ``_drain_stream_consumers`` name, and that module imports this one.
    from cuprum._line_stream import _discard_drain

    if run.tasks.stdin_task is not None:
        try:
            await run.tasks.stdin_task
        except BaseException:
            await _discard_drain(run, pid, execution)
            raise
    try:
        return await asyncio.gather(*run.tasks.consumers)
    except BaseException:
        await _discard_drain(run, pid, execution)
        raise
