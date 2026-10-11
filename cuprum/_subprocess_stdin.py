"""Subprocess stdin writing helpers.

Owns the ``cuprum.stdin`` logger and the stdin pipe lifecycle: writing
caller-provided bytes, closing the pipe, and emitting observable diagnostics
when the pipe fails early.

The bounded diagnostic the *other* standard-stream boundaries report through —
the producer, an invalid chunk, the encoder, opening a cuprum-owned target
file, and flushing a caller's borrowed file object — lives in
:mod:`cuprum._stdio_diagnostics` rather than here, because this module is at its
line-count budget and because those boundaries are reached from modules this
one does not import. Both emitters express the same rule that a diagnostic
never carries exception text, so a boundary added later inherits it rather than
re-deriving it.

The two sources differ in how much of the payload exists at once. A payload
(``StdinInput``) is already complete in memory, so it is written in one go;
that writer lives here. A producer (``StdinStream``) is pulled one chunk at a
time and is handled by ``cuprum._subprocess_stdin_stream``, which this module's
dispatcher starts when a caller supplies one. The dispatcher reads a resolved
:class:`~cuprum._stdio_plan._StdinPlan` rather than the caller's raw argument,
so the inherited/payload/producer decision is made once, when the stdio is
resolved, instead of being re-derived here.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from cuprum._pipeline_internals import _EventDetails, _StageObservation
from cuprum._stdio_plan import (
    _NoStdin,
    _PayloadStdin,
    _PipeStdin,
    _StdinPlan,
    _StreamStdin,
)

if typ.TYPE_CHECKING:
    from cuprum._subprocess_stdin_stream import _StdinCodec

_LOGGER = logging.getLogger("cuprum.stdin")

# How long a stdin writer is given to notice it should stop — both when the
# child it was feeding has exited and when the writer is being cancelled. Fixed
# and short for the same reason the readers' EOF grace is (see
# ``_CAPTURE_EOF_GRACE_S`` in ``cuprum._subprocess_wait``): the bytes are
# already written and the child that would read them is gone, so the only thing
# still being waited for is a pipe end held open by the child's own
# descendants. A writer that can finish at all finishes in well under this; one
# that cannot will never finish, however long it is given.
_STDIN_SETTLE_GRACE_S = 0.25


def _emit_stdin_error(
    process: asyncio.subprocess.Process,
    observation: _StageObservation,
    exc: BaseException,
    *,
    operation: str,
) -> None:
    """Emit an observable diagnostic for stdin pipe write failures.

    Bounded on purpose. The record names the failing operation and the
    exception's *class*; it does not carry the exception's message and does not
    attach a traceback. Both used to travel here, and both are caller data: an
    ``OSError`` from a pipe write quotes whatever the caller's own machinery
    put in it, and a logged traceback retains every local in the frames it
    passed through. The caller still sees the full failure — the observed
    event is a signal, not the channel that carries the error.
    """
    _LOGGER.error(
        "stdin_%s_failed pid=%s error=%s",
        operation,
        process.pid,
        type(exc).__name__,
        extra={
            "cuprum_pid": process.pid,
            "cuprum_stdin_operation": operation,
            "cuprum_error_type": type(exc).__name__,
        },
    )
    observation.emit(
        "stdin_error",
        _EventDetails(
            pid=process.pid,
            operation=operation,
            error_type=type(exc).__name__,
        ),
    )


async def _write_stdin(
    process: asyncio.subprocess.Process,
    stdin_data: bytes,
    observation: _StageObservation,
) -> None:
    """Write caller-provided stdin bytes and close the pipe.

    *stdin_data* is always a real ``bytes`` value: the only caller is
    :func:`_spawn_stdin_writer`, which returns before starting this task for an
    inherited stdin and routes a producer to the streaming writer instead. The
    payload is empty only for a run that explicitly asked for a stdin pipe and
    supplied no source, and writing nothing before closing is exactly the
    child-side view that request describes.
    """
    stdin = process.stdin
    if stdin is None:
        _LOGGER.debug("stdin_writer_skipped pid=%s reason=no_pipe", process.pid)
        return
    _LOGGER.debug(
        "stdin_writer_write_start pid=%s bytes=%s",
        process.pid,
        len(stdin_data),
        extra={"cuprum_pid": process.pid, "cuprum_stdin_bytes": len(stdin_data)},
    )
    try:
        stdin.write(stdin_data)
        await stdin.drain()
        observation.emit(
            "stdin",
            _EventDetails(pid=process.pid, byte_count=len(stdin_data)),
        )
    except (OSError, RuntimeError) as exc:
        _emit_stdin_error(process, observation, exc, operation="write")
    finally:
        await _close_stdin(process, stdin, observation)
    _LOGGER.debug("stdin_writer_finished pid=%s", process.pid)


async def _close_stdin(
    process: asyncio.subprocess.Process,
    stdin: asyncio.StreamWriter,
    observation: _StageObservation,
) -> None:
    """Close the child's stdin pipe, reporting any failure to close it.

    The wait is bounded, and deliberately so. ``wait_closed`` does not merely
    flush what cuprum wrote: it waits for the transport's ``connection_lost``,
    which needs the child-side pipe end to reach EOF. A grandchild that
    inherited the child's stdin read end holds that pipe open past the child's
    own exit, so the callback never fires and the wait never returns.

    That would be survivable if the wait were interruptible, but it is not:
    this coroutine runs inside the writer task's ``finally``, and a
    cancellation delivered there does not skip the ``await`` — it schedules
    one more cancellation, so ``close()`` still runs and the ``await`` still
    blocks. A caller could therefore cancel the writer and still wait forever
    for it, which is what turned a stalled run's deadline into an endless one.
    The descriptors themselves are released when the loop tears the transport
    down, so refusing to outlive the run costs no resource the run still owns.
    """
    try:
        _LOGGER.debug("stdin_writer_close_start pid=%s", process.pid)
        stdin.close()
        await asyncio.wait_for(stdin.wait_closed(), _STDIN_SETTLE_GRACE_S)
    except TimeoutError:
        # Ordered before the ``OSError`` handler deliberately: the builtin
        # ``TimeoutError`` that ``wait_for`` raises *is* an ``OSError``, so the
        # broader handler would otherwise report every expired window as a pipe
        # failure. It is not one: cuprum wrote everything it was given, and the
        # pipe's reader outliving the child is a fact about the child's own
        # descendants rather than a fault in this pipe.
        _LOGGER.debug(
            "stdin_writer_close_abandoned pid=%s reason=reader_still_open",
            process.pid,
        )
    except (OSError, RuntimeError) as exc:
        _emit_stdin_error(process, observation, exc, operation="close")


async def _cancel_stdin_writer(stdin_task: asyncio.Task[None] | None) -> None:
    """Cancel and drain a stdin writer task, tolerating any raised error.

    Used on the timeout and cancellation paths to reclaim a writer that may be
    blocked draining bytes into an unread pipe, so its cleanup cannot delay the
    surrounding failure work.

    Draining it is bounded: a task can absorb cancellation in its own
    ``finally`` and go on blocking there, which is exactly what the writer's
    pipe close used to do. Reclaiming a writer is meant to *bound* the wait for
    it, so an unbounded drain here would hand that guarantee back to the thing
    it was written to contain.
    """
    if stdin_task is None:
        return
    stdin_task.cancel()
    await asyncio.wait((stdin_task,), timeout=_STDIN_SETTLE_GRACE_S)


async def _settle_stdin_writer(stdin_task: asyncio.Task[None] | None) -> None:
    """Reclaim the stdin writer once the child it was feeding has exited.

    The exit path is the one place a writer can outlive what it writes to. A
    timeout or a cancellation reclaims the writer through
    :func:`_cancel_stdin_writer`, and an ordinary run has the producer exhaust
    and the writer close the pipe *before* the child is done — so by the time
    an exit settles, a writer that is still pending is one whose remaining work
    can reach nobody: the child is reaped and its end of the pipe is gone.

    Awaiting such a writer unconditionally is what this replaces. A producer
    parked in ``anext`` has no continuation that would ever end the wait, so an
    unbounded await made a stalled producer indistinguishable from a stalled
    child and outlasted the run's own deadline: the terminator fired, the child
    was killed, and the caller kept waiting on the producer.

    A writer that already finished is awaited, so a failure it raised before
    the exit settled still reaches the caller. One that is still pending is
    given the settle window, which is what lets a write already in flight fail
    with ``EPIPE`` and take the early-close path, and is then cancelled and
    reclaimed exactly as the teardown paths reclaim it. A producer that would
    have failed after that window loses the report, which is the same trade
    every other teardown path already makes; the alternative is a run that
    never ends.

    Parameters
    ----------
    stdin_task : asyncio.Task[None] | None
        The run's stdin writer. ``None`` for an inherited stdin, which has no
        pipe of cuprum's and so nothing to reclaim.

    Raises
    ------
    BaseException
        Whatever the writer raised, when it failed before or within the
        window. A writer this helper cancels reports nothing: it was reclaimed,
        not broken.
    """  # ruff: ignore[docstring-extraneous-exception] - the writer's own exception propagates.
    if stdin_task is None:
        return
    if not stdin_task.done():
        await asyncio.wait((stdin_task,), timeout=_STDIN_SETTLE_GRACE_S)
    if stdin_task.done():
        await stdin_task
        return
    await _cancel_stdin_writer(stdin_task)


def _spawn_payload_writer(
    process: asyncio.subprocess.Process,
    payload: bytes,
    observation: _StageObservation,
) -> asyncio.Task[None]:
    """Start the writer that sends one complete payload and closes the pipe.

    Both payload-shaped plan variants land here, and deliberately so: an
    explicitly requested empty stdin pipe and an empty payload are the same
    child-side view, so they share one writer rather than growing a second path
    that expresses the same thing twice.

    Returns
    -------
    asyncio.Task[None]
        The payload writer task, already scheduled.
    """
    _LOGGER.debug(
        "stdin_writer_task_start pid=%s bytes=%s",
        process.pid,
        len(payload),
        extra={"cuprum_pid": process.pid, "cuprum_stdin_bytes": len(payload)},
    )
    return asyncio.create_task(_write_stdin(process, payload, observation))


def _spawn_stdin_writer(
    process: asyncio.subprocess.Process,
    plan: _StdinPlan,
    codec: _StdinCodec,
    observation: _StageObservation,
) -> asyncio.Task[None] | None:
    """Start the stdin writer that a resolved plan calls for, if any.

    The plan decides, and its four variants map onto three outcomes. An
    inherited stdin needs no task: there is no pipe of cuprum's to write to or
    close. A producer gets the streaming writer, which owns its own backpressure
    and finalization. A payload — including the empty one that
    :class:`~cuprum._stdio_plan._PipeStdin` reduces to — goes to the payload
    writer, which writes what it was given and closes the pipe. Reusing that
    writer for an explicitly requested empty pipe is deliberate: the variant was
    reached by asking for a pipe and supplying no source, so the child should
    see exactly what an empty payload gives it, without a second code path
    expressing the same thing.

    Returns
    -------
    asyncio.Task[None] | None
        The writer task, or ``None`` for an inherited stdin, which has no pipe
        of cuprum's to write to or close.
    """
    # The streaming writer imports this module back for the pipe primitives, so
    # a module-level import of it would close a cycle at load time, when neither
    # module is complete. Deferring the import also keeps the name looked up on
    # the module, which is where the test suite's monkeypatch seams replace it.
    from cuprum._subprocess_stdin_stream import _write_stdin_stream

    match plan:
        case _NoStdin():
            # An inherited stdin has no pipe of cuprum's, so no writer and
            # nothing to close: the child uses the parent's own descriptor.
            return None
        case _StreamStdin(stream=producer):
            _LOGGER.debug("stdin_writer_task_start pid=%s source=stream", process.pid)
            return asyncio.create_task(
                _write_stdin_stream(process, producer, codec, observation)
            )
        case _PipeStdin():
            # Requested a pipe, supplied no source: the child should see exactly
            # what an empty payload gives it, so the payload writer is reused
            # rather than a second code path expressing the same thing.
            return _spawn_payload_writer(process, b"", observation)
        case _PayloadStdin(data=payload):
            return _spawn_payload_writer(process, payload, observation)


__all__ = [
    "_cancel_stdin_writer",
    "_close_stdin",
    "_emit_stdin_error",
    "_settle_stdin_writer",
    "_spawn_stdin_writer",
    "_write_stdin",
]
