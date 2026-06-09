"""Pipeline stage stream setup and capture helpers."""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum._streams import _consume_stream

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum._pipeline_internals import _StageObservation


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
    """Determine stream file descriptors for a pipeline stage."""
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
            from cuprum._pipeline_internals import _EventDetails

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
            from cuprum._pipeline_internals import _EventDetails

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
