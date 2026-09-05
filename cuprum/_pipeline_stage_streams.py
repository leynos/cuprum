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

from cuprum._streams import _consume_stream, _RelayDiagnostics
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
    capture_or_echo: bool,
) -> _StageStreamConfig:
    """Select PIPE/DEVNULL fds for stdin, stdout, and stderr by position and mode."""
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
    """Create stderr and stdout capture tasks for a pipeline stage.

    Returns
    -------
    tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None, tuple[_RelayDiagnostics | None, _RelayDiagnostics | None]]
        The stderr and stdout capture tasks, together with the per-stream
        relay diagnostics collectors (stderr first, stdout second, either
        ``None`` when that stream has no capture task). The caller retains
        them with the spawn state so the pipeline's result builders can read
        each stage's diagnostics from its own streams.
    """
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if not config.capture_or_echo:
        return stderr_task, stdout_task, (None, None)

    stderr_on_line: cabc.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stderr_on_line(line: str) -> None:
            """Emit a stderr observe event for each captured line."""
            observation.emit(
                "stderr",
                _EventDetails(pid=process.pid, line=line),
            )

    stderr_relay_diagnostics = _RelayDiagnostics()
    stderr_task = asyncio.create_task(
        _consume_stream(
            process.stderr,
            dc.replace(
                config.stream_config,
                sink=config.stderr_sink,
                stream=EchoStream.STDERR,
            ),
            on_line=stderr_on_line,
            relay_diagnostics=stderr_relay_diagnostics,
        ),
    )

    if not is_last_stage:
        return stderr_task, stdout_task, (stderr_relay_diagnostics, None)

    stdout_on_line: cabc.Callable[[str], None] | None = None
    if observation.hooks.observe_hooks:

        def stdout_on_line(line: str) -> None:
            """Emit a stdout observe event for each captured line."""
            observation.emit(
                "stdout",
                _EventDetails(pid=process.pid, line=line),
            )

    stdout_relay_diagnostics = _RelayDiagnostics()
    stdout_task = asyncio.create_task(
        _consume_stream(
            process.stdout,
            config.stream_config,
            on_line=stdout_on_line,
            relay_diagnostics=stdout_relay_diagnostics,
        ),
    )

    return (
        stderr_task,
        stdout_task,
        (stderr_relay_diagnostics, stdout_relay_diagnostics),
    )
