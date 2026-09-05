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

from cuprum._line_callbacks import _compose_line_callbacks, _LineEmissionContext
from cuprum._streams import _consume_stream
from cuprum.echo_events import EchoStream

if typ.TYPE_CHECKING:
    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_types import _StageObservation


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


@dc.dataclass(frozen=True, slots=True)
class _StageCaptureRequest:
    """Everything the capture-task builder needs for one stage.

    Attributes
    ----------
    process:
        The stage's subprocess, whose streams are consumed.
    config:
        The pipeline run config owning the stream and echo settings.
    observation:
        The stage's observation, carrying the observe-hook set.
    is_last_stage:
        Whether this stage's stdout is the pipeline's final output.
    started_at:
        Monotonic spawn reference for the stage's line stamps.

    """

    process: asyncio.subprocess.Process
    config: _PipelineRunConfig
    observation: _StageObservation
    is_last_stage: bool
    started_at: float


def _create_stage_capture_tasks(
    request: _StageCaptureRequest,
) -> tuple[asyncio.Task[str | None] | None, asyncio.Task[str | None] | None]:
    """Create stderr and stdout capture tasks for a pipeline stage."""
    process = request.process
    config = request.config
    observation = request.observation
    stderr_task: asyncio.Task[str | None] | None = None
    stdout_task: asyncio.Task[str | None] | None = None

    if not request.config.capture_or_echo:
        return stderr_task, stdout_task

    # Interior stages' stdout is not observed for lines: it is consumed by the
    # next stage, so no stage other than the last ever owns a stdout consumer.
    stderr_on_line = _compose_line_callbacks(
        observation,
        _LineEmissionContext(
            stream="stderr",
            pid=process.pid,
            on_line=None,
            started_at=request.started_at,
        ),
    )

    stderr_task = asyncio.create_task(
        _consume_stream(
            process.stderr,
            dc.replace(
                config.stream_config,
                sink=config.stderr_sink,
                stream=EchoStream.STDERR,
            ),
            on_line=stderr_on_line,
        ),
    )

    if not request.is_last_stage:
        return stderr_task, stdout_task

    stdout_on_line = _compose_line_callbacks(
        observation,
        _LineEmissionContext(
            stream="stdout",
            pid=process.pid,
            on_line=None,
            started_at=request.started_at,
        ),
    )

    stdout_task = asyncio.create_task(
        _consume_stream(
            process.stdout,
            config.stream_config,
            on_line=stdout_on_line,
        ),
    )

    return stderr_task, stdout_task
