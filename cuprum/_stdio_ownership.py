"""Mapping a resolved stdio onto the spawn layer, and owning what cuprum opened.

Split from ``cuprum._subprocess_spawn`` when the rebase onto ``main`` pushed
that module past the 400-line ceiling. The seam is the one its docstring
already drew: this module owns *how a binding becomes a value the spawn layer
receives and who closes it*, while ``_subprocess_spawn`` owns *when* those
steps run relative to the fork and what the spawned child becomes.

The ownership rule implemented here is narrow on purpose: **only
library-owned resources are closed.** A path target opens the file, hands the
descriptor to the child, and closes cuprum's copy in a ``finally`` immediately
after the spawn returns. A borrowed descriptor or file object is never closed —
the caller's own use of it afterwards is the only witness that the rule held,
since no exit code can tell a close cuprum owed from a close it did not.

A borrowed file object is nonetheless *flushed* here, immediately before the
fork, by :func:`_flush_borrowed_stdio`. That is not a contradiction of the
rule: a flush pushes the caller's own buffered bytes through the caller's own
descriptor and changes nothing about who closes it. It belongs with the opens
rather than with the resolver because resolution and the fork are the same
instant only on the ``run()`` path; ``lines()`` resolves when it is called and
forks at first iteration, so a resolver-side flush would miss anything written
in between.

The stdio mapping is where a borrowed descriptor and a library-owned one are
told apart, and that distinction is the whole point of the module. ``Popen``
treats ``PIPE``, ``DEVNULL``, a raw ``int``, and a file object differently, but
it exposes a parent-side stream object only for ``PIPE``: a raw descriptor, a
file object, and ``DEVNULL`` alike leave ``Popen.stdout`` ``None`` (measured,
not assumed), so the ``wait4`` backend cannot recover pipe-ness from the child
object — it would have to guess from the value it computed, and the value has
already folded a non-consuming pipe down to ``DEVNULL``.

The execution bundle these helpers read is imported only under
``TYPE_CHECKING``: every function here takes it as a parameter and reaches its
attributes, so nothing needs the class object at runtime, and a real import
would close a cycle with the module that calls them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import typing as typ

from cuprum._stdio_diagnostics import _emit_stdio_error, _StdioFailure
from cuprum._stdio_plan import _NoStdin, _open_owned_path
from cuprum.stdio_events import StdioFailureCategory

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._constants import PipeStream
    from cuprum._stdio_plan import _StdinPlan, _StdioBinding
    from cuprum._subprocess_spawn import _SubprocessExecution

_LOGGER = logging.getLogger(__name__)


def _output_stdio(
    binding: _StdioBinding,
    owned: int | None,
    *,
    consumes: bool,
) -> int | None:
    """Map one resolved output binding onto the value ``Popen`` receives.

    Three outcomes, and the third is the one that matters: a stream cuprum
    needs to read becomes ``PIPE``; a stream nobody reads becomes ``DEVNULL``
    rather than the parent's own descriptor, which is what keeps a run from
    echoing an unread child's output into the caller's terminal; and a stream
    the caller bound somewhere else is passed through as its own descriptor.

    Parameters
    ----------
    consumes : bool
        Whether anything in this run still needs to read the stream.
    binding : _StdioBinding
        The resolved binding for that stream.
    owned : int | None
        The descriptor cuprum opened for this stream, when the binding names a
        file to own. A borrowed binding carries its descriptor already, so this
        is ``None`` for every other variant.

    Returns
    -------
    int | None
        ``PIPE``, ``DEVNULL``, the borrowed or owned descriptor, or ``None``
        for an inherited stream.
    """
    if binding.is_pipe:
        return asyncio.subprocess.PIPE if consumes else asyncio.subprocess.DEVNULL
    if owned is not None:
        return owned
    return binding.descriptor


def _stdin_stdio(plan: _StdinPlan) -> int | None:
    """Map a resolved stdin plan onto the value ``Popen`` receives."""
    return None if isinstance(plan, _NoStdin) else asyncio.subprocess.PIPE


def _open_owned_stdio(execution: _SubprocessExecution) -> dict[str, int]:
    """Open every cuprum-owned target file, immediately before the spawn.

    The returned mapping is both the record of what to close and what to pass:
    an owned path has no descriptor until this call, so a binding that names
    one contributes ``None`` to the stdio config and takes the descriptor
    opened here instead.

    A failure part-way through undoes what it already did before re-raising.
    The ``finally`` that closes owned descriptors in
    :func:`~cuprum._subprocess_spawn._spawn_subprocess` wraps the *spawn*, so a
    binding that fails to open never reaches it — and without this cleanup the
    streams opened before the failing one would leak, one descriptor per failed
    run. Both streams can name an owned path, so the partial case is reachable
    rather than theoretical.

    Returns
    -------
    dict[str, int]
        The descriptor cuprum owns, by stream name.

    Raises
    ------
    OSError
        If a target file cannot be opened, as raised by
        :func:`~cuprum._stdio_plan._open_owned_path` and propagated from here
        so the spawn never begins with a half-open stdio set.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _open_owned_path.
    opened: dict[str, int] = {}
    try:
        for binding in (execution.stdio.stdout, execution.stdio.stderr):
            if binding.owned_path is not None:
                opened[binding.stream] = _emit_owned_path_open(
                    execution, binding.stream, binding.owned_path
                )
    except BaseException:
        # Opened in the streams' own order, so whatever is recorded here was
        # opened by this call and is cuprum's to close. Re-raised unchanged:
        # the open failure is what the caller needs to see, not that its
        # cleanup also ran.
        _close_owned_stdio(opened)
        raise
    return opened


def _emit_owned_path_open(
    execution: _SubprocessExecution,
    stream: PipeStream,
    path: Path,
) -> int:
    """Open one owned target, reporting a failure without the failing path.

    The diagnostic and the exception deliberately disagree about the path. The
    ``OSError`` :func:`~cuprum._stdio_plan._open_owned_path` raises names the
    file, because a caller with two redirected streams needs to know which one
    failed; the diagnostic omits it, because it is caller data and this is an
    operational signal rather than the error channel. Both the exception and
    its chained cause propagate unchanged.

    ``pid`` is ``None``: this runs immediately before the fork, so no child
    exists to name.

    Parameters
    ----------
    execution : _SubprocessExecution
        The run this binding belongs to, carrying the observation the
        diagnostic is emitted through.
    stream : PipeStream
        Which of the child's streams the file is bound to. The value names the
        boundary in the ``OSError`` ``_open_owned_path`` raises; the diagnostic
        deliberately omits it.
    path : Path
        The caller-named file to open.

    Returns
    -------
    int
        The open descriptor, which cuprum owns and must close.

    Raises
    ------
    OSError
        If the target file cannot be opened, propagated from
        :func:`~cuprum._stdio_plan._open_owned_path`.
    """
    try:
        return _open_owned_path(stream, path)
    except OSError as exc:
        _emit_stdio_error(
            execution.observation,
            _StdioFailure(
                category=StdioFailureCategory.OWNED_PATH_OPEN,
                operation="open",
                error_type=type(exc).__name__,
                pid=None,
            ),
        )
        raise


def _flush_borrowed_stdio(execution: _SubprocessExecution) -> None:
    """Push a borrowed file object's buffered bytes out, immediately pre-fork.

    The flush has to happen here rather than where the binding was resolved.
    Resolution runs when the run is prepared, and on the ``lines()`` path that
    is when ``lines()`` is called, not when the child is spawned; the caller
    can legitimately write to the borrowed object in between and expect those
    bytes to reach the child. Flushing at resolution would push only what was
    written before the call and silently drop the rest — measured, not
    assumed: the child read ``b'first '`` while the file held
    ``b'first second '``.

    Only a borrowed *file object* has anything to flush; a borrowed ``int``
    descriptor has no buffer cuprum can reach, and a library-owned pipe or path
    has no caller-side buffer to push.

    A flush that fails is diagnosed and then re-raised, because proceeding
    would silently start the child without the bytes the caller buffered — a
    wrong result delivered with no error at all. The reporting is bounded like
    every other stdio diagnostic: the caller's object is named by stream only,
    and ``pid`` is ``None`` because the fork has not happened yet.

    Parameters
    ----------
    execution : _SubprocessExecution
        The run being spawned; its stdout and stderr bindings are consulted.

    Raises
    ------
    Exception
        Whatever the borrowed object's ``flush`` raised, unchanged.
    """  # ruff: ignore[docstring-extraneous-exception] - the borrowed object's own failure propagates.
    for binding in (execution.stdio.stdout, execution.stdio.stderr):
        borrowed = binding.borrowed
        if borrowed is None:
            continue
        try:
            borrowed.flush()
        except Exception as exc:
            _emit_stdio_error(
                execution.observation,
                _StdioFailure(
                    category=StdioFailureCategory.BORROWED_FLUSH,
                    operation="flush",
                    error_type=type(exc).__name__,
                    pid=None,
                ),
            )
            raise


def _close_owned_stdio(opened: dict[str, int]) -> None:
    """Close cuprum's copy of each owned descriptor, once the child has one.

    Reached from a ``finally`` that runs after the spawn call returned, so the
    child has already forked and inherited the descriptor; closing earlier
    would hand it a stale one. A failure to close is reported rather than
    raised: the child is already running and the spawn's own failure, if any,
    is the one that must reach the caller.

    The record is bounded like the other stdio diagnostics. It names the stream
    and the exception class, and deliberately attaches no traceback: a logged
    traceback retains every local in the frames it passed through, and those
    frames hold the caller's own path and file object.

    Parameters
    ----------
    opened : dict[str, int]
        The descriptors opened for this spawn, by stream name.
    """
    for stream, fd in opened.items():
        try:
            os.close(fd)
        except OSError as exc:
            _LOGGER.warning(
                "stdio_close_failed stream=%s error=%s",
                stream,
                type(exc).__name__,
                extra={
                    "cuprum_stream": stream,
                    "cuprum_error_type": type(exc).__name__,
                },
            )


__all__ = [
    "_close_owned_stdio",
    "_flush_borrowed_stdio",
    "_open_owned_stdio",
    "_output_stdio",
    "_stdin_stdio",
]
