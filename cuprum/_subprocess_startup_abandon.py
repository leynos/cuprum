"""Reclaim a child whose startup failed after its spawn but before hand-back.

A spawned child belongs to the run only once the setup that follows the spawn
has returned. Several steps in that setup can fail: the ``start`` event runs
synchronous observe hooks inline and re-raises their failures, the streamed run
prepares and starts its consumers, and the direct run creates its stdin writer.
A failure in any of them escapes the helper that spawned the child, so every
route that would normally wait for, drain, and reap it is never entered. This
module owns the reclaim those routes share.

The sequence is fixed, and each step is given its own turn rather than
aborting the rest:

1. terminate the child and reap it, signalling its process group when the run
   owns one. Reaping is not incidental: a stop that only kills still leaves a
   child for this parent to collect.
2. cancel every task the failed setup had already started and gather them, so
   no writer or reader outlives the attempt that failed.
3. release the pipes: close stdin, then read stdout and stderr to end-of-file
   under the capture drain's own EOF grace. Reading a stream is what lets its
   parent-side transport disconnect, and the wait is bounded because a
   descendant that inherited a pipe can outlive the group teardown — the limit
   ADR 021 records.
4. release the capture accumulators, now that no consumer can still be writing
   into them. A drain cancelled rather than settled never builds the result
   that would have carried its spill handle, so the accumulator is the last
   reference to a temporary file the library owns.

Ordinary cleanup failures are suppressed per step, so a later step still runs
and the failure owed to the caller — the hook or setup step that raised —
reaches it unchanged. A cancellation is not suppressed; it propagates once the
sequence has completed, which is why the whole sequence runs inside one
``_shielded_cleanup`` rather than three sequential ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

from cuprum._capture_cleanup import _cleanup_capture_sinks
from cuprum._process_lifecycle import _shielded_cleanup, _terminate_all_shielded
from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S
from cuprum._teardown_policy import _TeardownPolicy

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution

# Chunk size for the discard read. Reading to EOF in bounded pieces rather than
# with a bare ``read()`` keeps a child that wrote a large backlog before it was
# stopped from being copied into memory all at once; the bytes are discarded.
_DISCARD_CHUNK_BYTES = 64 * 1024


def _record_started(
    recorded: list[asyncio.Task[typ.Any]] | None,
    task: asyncio.Task[typ.Any] | None,
) -> None:
    """Record one started task, so a later startup failure can reclaim it."""
    if recorded is not None and task is not None:
        recorded.append(task)


async def _abandon_started_process(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    tasks: cabc.Sequence[asyncio.Task[typ.Any]] = (),
    *,
    captures: cabc.Iterable[CaptureSink | None] = (),
) -> None:
    """Stop, reap, and release a child whose startup failed after the spawn.

    Parameters
    ----------
    process : asyncio.subprocess.Process
        The child the spawn returned. It is still this run's to reclaim even
        though no caller was ever handed it.
    execution : _SubprocessExecution
        The run's bundle, read for the teardown grace, the process-group
        ownership that decides what a signal targets, the observation a refused
        delivery is reported against, and the stdin plan its pipe is closed
        from.
    tasks : collections.abc.Sequence[asyncio.Task[typing.Any]]
        Tasks the failed setup had already started, in creation order. Empty
        when the failure happened before any task existed.
    captures : collections.abc.Iterable[CaptureSink | None]
        The capture accumulators the failed setup had resolved, if it got far
        enough to resolve them. Their spill files are removed after the tasks
        are settled, because a cancelled drain never builds the result that
        would otherwise have carried the handle. Empty when the run captures
        nothing or never reached the point of resolving its streams.

    Notes
    -----
    Nothing here raises for an ordinary failure of its own: the error the
    caller is already unwinding is the one it re-raises, and a teardown that
    replaced it would misreport why the run ended. A cancellation still
    propagates, but only after every step has run.
    """
    recorded = tuple(tasks)
    sinks = tuple(captures)

    async def abandon() -> None:
        """Run the whole reclaim as one sequence behind a single shield."""
        await _stop_child(process, execution)
        await _settle_recorded_tasks(recorded)
        await _release_pipes(process, execution)
        await _release_captures(sinks)

    await _shielded_cleanup(abandon())


async def _stop_child(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Terminate and reap the child, signalling its group when the run owns one."""
    with contextlib.suppress(Exception):
        await _terminate_all_shielded(
            (process,),
            _TeardownPolicy(
                execution.ctx.cancel_grace,
                owns_group=execution.owns_process_group,
            ),
            observations=(execution.observation,),
        )


async def _settle_recorded_tasks(
    tasks: cabc.Sequence[asyncio.Task[typ.Any]],
) -> None:
    """Cancel and drain the tasks the failed setup had already started."""
    if not tasks:
        return
    with contextlib.suppress(Exception):
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _release_pipes(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
) -> None:
    """Close stdin, then drain stdout and stderr so their pipes are released."""
    # Imported here rather than at module scope: the stdin module reaches the
    # pipeline internals, and the spawn module that imports this one sits on a
    # shorter load path than that.
    from cuprum._subprocess_stdin import _close_stdin

    stdin = process.stdin
    # A writer task that was cancelled has already closed the pipe in its own
    # ``finally``; asking again would report the second close as a pipe error.
    if stdin is not None and not stdin.is_closing():
        with contextlib.suppress(Exception):
            await _close_stdin(process, stdin, execution.observation)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(_CAPTURE_EOF_GRACE_S):
                    await _discard_to_eof(stream)


async def _release_captures(
    captures: cabc.Iterable[CaptureSink | None],
) -> None:
    """Remove any spill file the started consumers had opened.

    Runs after the tasks are settled: a consumer still draining into its
    accumulator could otherwise recreate the file this is removing.
    """
    with contextlib.suppress(Exception):
        await _cleanup_capture_sinks(captures)


async def _discard_to_eof(stream: asyncio.StreamReader) -> None:
    """Read a stream to end-of-file, discarding it, in bounded pieces."""
    chunk = await stream.read(_DISCARD_CHUNK_BYTES)
    while chunk:
        chunk = await stream.read(_DISCARD_CHUNK_BYTES)


__all__ = ["_abandon_started_process", "_record_started"]
