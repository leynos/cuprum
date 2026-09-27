"""Spawning one child with the run's resolved stdio, and owning what it borrows.

Everything the parent does *around* a spawn without consuming a stream lives
here: mapping a resolved stdio into the value the spawn layer receives, opening
each cuprum-owned target file immediately before the fork, closing cuprum's copy
of it immediately after, and the spawn call itself.

The stdio mapping is where a borrowed descriptor and a library-owned one are
told apart, and that distinction is the whole point of the module. ``Popen``
treats ``PIPE``, ``DEVNULL``, a raw ``int``, and a file object differently, and
it leaves its own ``stdin``/``stdout``/``stderr`` as ``None`` for everything
that is not a pipe — so the ``wait4`` backend could infer pipe-ness. The
``create_subprocess_exec`` fallback would instead attach a reader to whatever it
was handed, including a descriptor the caller owns. ``pipes`` names the streams
cuprum really holds a parent-side pipe for, which is the one piece of the
resolution the value cannot express for itself.

The ownership rule the module implements is narrow on purpose: **only
library-owned resources are closed.** A path target opens the file, hands the
descriptor to the child, and closes cuprum's copy in a ``finally`` immediately
after the spawn returns. A borrowed descriptor or file object is never closed —
the caller's own use of it afterwards is the only witness that the rule held,
since no exit code can tell a close cuprum owed from a close it did not.
"""

from __future__ import annotations

import asyncio
import logging
import os
import typing as typ

from cuprum import _wait4_process
from cuprum._process_lifecycle import _merge_env
from cuprum._stdio_plan import (
    _NoStdin,
    _open_owned_path,
    _StdinPlan,
    _StdioBinding,
)
from cuprum._subprocess_context import _cwd_arg

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution

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
    for binding in (execution.stdio.stdout, execution.stdio.stderr):
        if binding.owned_path is not None:
            opened[binding.stream] = _open_owned_path(
                binding.stream, binding.owned_path
            )
    return opened


def _close_owned_stdio(opened: dict[str, int]) -> None:
    """Close cuprum's copy of each owned descriptor, once the child has one.

    Reached from a ``finally`` that runs after the spawn call returned, so the
    child has already forked and inherited the descriptor; closing earlier
    would hand it a stale one. A failure to close is reported rather than
    raised: the child is already running and the spawn's own failure, if any,
    is the one that must reach the caller.

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
                exc_info=exc,
                extra={
                    "cuprum_stream": stream,
                    "cuprum_error_type": type(exc).__name__,
                },
            )


async def _spawn_subprocess(
    execution: _SubprocessExecution,
) -> asyncio.subprocess.Process:
    """Spawn an async subprocess with the run's resolved stdio and environment.

    The config the spawn layer receives is deliberately a superset of what
    ``Popen`` consumes: ``pipes`` names the streams cuprum holds a parent-side
    pipe for, which is information the value cannot express. ``Popen`` leaves
    its own ``stdin``/``stdout``/``stderr`` as ``None`` for anything that is not
    a pipe, so the ``wait4`` path could infer pipe-ness; the
    ``create_subprocess_exec`` fallback would instead attach a reader to
    whatever it was handed, including a borrowed descriptor. Naming the pipes
    explicitly is what makes both paths agree.

    Returns
    -------
    asyncio.subprocess.Process
        The spawned child.

    Raises
    ------
    OSError
        If an owned target file cannot be opened, as raised by
        :func:`_open_owned_stdio` before the spawn call is made.
    """  # ruff: ignore[docstring-extraneous-exception] - OSError propagates from _open_owned_stdio.
    # Opening before the spawn and closing in the ``finally`` is the whole
    # lifetime of an owned descriptor: it exists for exactly as long as it takes
    # the child to inherit it. A borrowed binding carries no entry in ``opened``,
    # so it reaches the spawn without ever being recorded as something to close.
    opened = _open_owned_stdio(execution)
    try:
        return await _wait4_process.spawn_direct_process(
            _wait4_process.DirectProcessConfig(
                argv=execution.cmd.argv_with_program,
                stdin=_stdin_stdio(execution.stdio.stdin),
                stdout=_output_stdio(
                    execution.stdio.stdout,
                    opened.get(execution.stdio.stdout.stream),
                    consumes=execution.consumes_stdout,
                ),
                stderr=_output_stdio(
                    execution.stdio.stderr,
                    opened.get(execution.stdio.stderr.stream),
                    consumes=execution.consumes_stderr,
                ),
                pipes=execution.pipes,
                env=_merge_env(execution.ctx.env, execution.ctx.env_mode),
                cwd=_cwd_arg(execution.ctx.cwd),
            )
        )
    finally:
        _close_owned_stdio(opened)


__all__ = [
    "_open_owned_stdio",
    "_output_stdio",
    "_spawn_subprocess",
    "_stdin_stdio",
]
