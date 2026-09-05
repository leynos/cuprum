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
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._pipeline_types import _EventDetails, _StageObservation


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
    stdout_capture_or_echo: bool,
    stderr_capture_or_echo: bool,
) -> _StageStreamConfig:
    """Select PIPE/DEVNULL fds for stdin, stdout, and stderr by position and mode.

    A non-final stage always pipes stdout so its output can relay into the
    next stage's stdin, regardless of capture or echo. The final stage's
    stdout and every stage's stderr follow their own capture-or-echo gate.

    Returns
    -------
    _StageStreamConfig
        The stdin, stdout, and stderr FD flags for the stage position.
    """
    stdin = asyncio.subprocess.DEVNULL if idx == 0 else asyncio.subprocess.PIPE
    stdout = (
        asyncio.subprocess.PIPE
        if idx != last_idx or stdout_capture_or_echo
        else asyncio.subprocess.DEVNULL
    )
    stderr = (
        asyncio.subprocess.PIPE
        if stderr_capture_or_echo
        else asyncio.subprocess.DEVNULL
    )
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

    stderr_on_line: cabc.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stderr_on_line(line: str) -> None:
            """Emit a stderr observe event for each captured line."""
            observation.emit(
                "stderr",
                _EventDetails(pid=process.pid, line=line),
            )

    if config.stderr_capture_or_echo:
        stderr_task = asyncio.create_task(
            _consume_stream(
                process.stderr,
                dc.replace(
                    config.stderr_stream_config,
                    stream=EchoStream.STDERR,
                ),
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

    if config.stdout_capture_or_echo:
        stdout_task = asyncio.create_task(
            _consume_stream(
                process.stdout,
                config.stream_config,
                on_line=stdout_on_line,
            ),
        )

    return stderr_task, stdout_task
