"""Creation of context-correlated inter-stage pump tasks."""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import typing as typ

from cuprum.pump_observation import _correlate_pump_events

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation


type _PipeDispatcher = cabc.Callable[
    [asyncio.StreamReader | None, asyncio.StreamWriter | None],
    cabc.Awaitable[None],
]


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
    observations: tuple[_StageObservation, ...],
    dispatch: _PipeDispatcher,
) -> list[asyncio.Task[None]]:
    """Create one context-correlated task for each adjacent stage pair."""
    return [
        _create_pipe_task(
            processes[idx].stdout,
            processes[idx + 1].stdin,
            observations[idx] if idx < len(observations) else None,
            dispatch,
        )
        for idx in range(len(processes) - 1)
    ]


def _create_pipe_task(
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
    observation: _StageObservation | None,
    dispatch: _PipeDispatcher,
) -> asyncio.Task[None]:
    """Create a pump task inheriting its upstream stage's execution token."""
    if observation is None:
        return asyncio.create_task(_dispatch_pipe(dispatch, reader, writer))
    with _correlate_pump_events(observation.exec_id):
        return asyncio.create_task(_dispatch_pipe(dispatch, reader, writer))


async def _dispatch_pipe(
    dispatch: _PipeDispatcher,
    reader: asyncio.StreamReader | None,
    writer: asyncio.StreamWriter | None,
) -> None:
    """Await the dispatch seam from a coroutine suitable for task creation."""
    await dispatch(reader, writer)
