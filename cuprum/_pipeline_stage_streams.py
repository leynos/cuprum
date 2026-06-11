"""Canonical stream policy and capture-task helpers for pipeline stages.

This module owns the pipeline-specific PIPE-versus-DEVNULL decision used while
spawning subprocess stages. ``cuprum._process_lifecycle`` asks
``_get_stage_stream_fds`` for each stage's stdio handles before it calls
``asyncio.create_subprocess_exec``, while ``cuprum._pipeline_streams`` re-exports
the capture-task helper used after each process is started.

Keep stdio policy changes here so the process lifecycle code stays focused on
starting, observing, waiting for, and cleaning up subprocesses rather than
duplicating stream-selection rules inline.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._streams import _consume_stream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_internals import _StageObservation
from cuprum._pipeline_types import _EventDetails


@dc.dataclass(frozen=True, slots=True)
class _StageStreamConfig:
    """Stream file descriptor configuration for a pipeline stage."""

    stdin: int
    stdout: int
    stderr: int


def _get_stage_stream_fds(
    idx: int,
    last_idx: int,
    *,
    capture_or_echo: bool,
) -> _StageStreamConfig:
    """Determine stream file descriptors for a pipeline stage.

    This is the canonical PIPE-versus-DEVNULL stdio policy for pipeline
    spawning; ``_spawn_pipeline_processes`` must route through it rather
    than re-deriving the flags inline. The policy is:

    - ``stdin``: the first stage reads from ``DEVNULL``; later stages read
      from a ``PIPE`` fed by the previous stage.
    - ``stdout``: intermediate stages always write to a ``PIPE`` (the next
      stage's stdin); the final stage writes to a ``PIPE`` only when output
      is captured or echoed, otherwise ``DEVNULL``.
    - ``stderr``: a ``PIPE`` when output is captured or echoed, otherwise
      ``DEVNULL``.

    Example
    -------
    >>> fds = _get_stage_stream_fds(0, 1, capture_or_echo=False)
    >>> (fds.stdin, fds.stdout, fds.stderr) == (
    ...     asyncio.subprocess.DEVNULL,
    ...     asyncio.subprocess.PIPE,
    ...     asyncio.subprocess.DEVNULL,
    ... )
    True
    """
    stdin = asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
    stdout = (
        asyncio.subprocess.PIPE
        if idx != last_idx or capture_or_echo
        else asyncio.subprocess.DEVNULL
    )
    stderr = asyncio.subprocess.PIPE if capture_or_echo else asyncio.subprocess.DEVNULL
    return _StageStreamConfig(stdin=stdin, stdout=stdout, stderr=stderr)


def _create_stage_capture_tasks(
    process: asyncio.subprocess.Process,
    config: _PipelineRunConfig,
    *,
    is_last_stage: bool,
    observation: _StageObservation,
) -> tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None]:
    """Create stderr and stdout capture tasks for a pipeline stage."""
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if not config.capture_or_echo:
        return stderr_task, stdout_task

    stderr_on_line: cabc.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stderr_on_line(line: str) -> None:
            """Emit a stderr observe event for each captured line."""
            observation.emit(
                "stderr",
                _EventDetails(pid=process.pid, line=line),
            )

    stderr_task = asyncio.create_task(
        _consume_stream(
            process.stderr,
            dc.replace(config.stream_config, sink=config.stderr_sink),
            on_line=stderr_on_line,
        ),
    )

    if not is_last_stage:
        return stderr_task, stdout_task

    stdout_on_line: cabc.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stdout_on_line(line: str) -> None:
            """Emit a stdout observe event for each captured line."""
            observation.emit(
                "stdout",
                _EventDetails(pid=process.pid, line=line),
            )

    stdout_task = asyncio.create_task(
        _consume_stream(
            process.stdout,
            config.stream_config,
            on_line=stdout_on_line,
        ),
    )

    return stderr_task, stdout_task
