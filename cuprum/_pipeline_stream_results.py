"""Await, cancel, and collect results from pipeline stream tasks.

This module owns the teardown and result-collection half of pipeline stream
handling. Once ``cuprum._pipeline_streams`` has created the capture and pump
tasks, these helpers gather their results, cancel the consumer tasks during
cleanup, and surface any unexpected pipe failures. They deliberately hold no
knowledge of how the tasks were created or which backend pumps the bytes; they
operate purely on the resulting :class:`asyncio.Task` objects.
"""

from __future__ import annotations

import asyncio


def _flatten_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> list[asyncio.Task[str | None]]:
    """Collect all running stream consumer tasks for cancellation cleanup."""
    tasks = [task for task in stderr_tasks if task is not None]
    if stdout_task is not None:
        tasks.append(stdout_task)
    return tasks


async def _cancel_stream_tasks(
    stderr_tasks: list[asyncio.Task[str | None] | None],
    stdout_task: asyncio.Task[str | None] | None,
) -> None:
    """Cancel stream consumer tasks and await their completion."""
    tasks = _flatten_stream_tasks(stderr_tasks, stdout_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _gather_optional_text_tasks(
    tasks: list[asyncio.Task[str | None] | None],
) -> tuple[str | None, ...]:
    """Await optional capture tasks, returning a tuple aligned with inputs."""
    return tuple(
        await asyncio.gather(
            *(
                task if task is not None else asyncio.sleep(0, result=None)
                for task in tasks
            ),
        ),
    )


async def _collect_pipe_results(
    pipe_tasks: list[asyncio.Task[None]],
) -> list[object]:
    """Collect pipe task results, capturing exceptions rather than raising them.

    Uses return_exceptions=True to gather all results including any exceptions
    that occurred during pipe streaming between pipeline stages.

    Returns
    -------
    list[object]
        Each pipe task's result, or the exception it raised.
    """
    return list(await asyncio.gather(*pipe_tasks, return_exceptions=True))


async def _reconcile_pipe_tasks(pipe_tasks: list[asyncio.Task[None]]) -> None:
    """Cancel and drain the inter-stage pumps after a pipeline deadline.

    Safe to run whether or not ``_wait_for_pipeline`` already reconciled them:
    cancelling a finished task is a no-op and gathering an already-gathered one
    returns its recorded outcome. Failures are absorbed because a
    ``TimeoutExpired`` is already propagating and a broken pump must not
    replace it.
    """
    for task in pipe_tasks:
        if not task.done():
            task.cancel()
    await _collect_pipe_results(pipe_tasks)


def _surface_unexpected_pipe_failures(pipe_results: list[object]) -> None:
    """Raise non-BrokenPipe exceptions from pipe results.

    BrokenPipeError and ConnectionResetError are expected when downstream
    processes terminate early (e.g., head) and should not fail the pipeline.
    Other exceptions indicate genuine failures and must be surfaced.

    The final case matches ``BaseException`` rather than ``Exception`` so a
    cancelled pipe task cannot be dropped: ``asyncio.CancelledError`` derives
    from ``BaseException``, and ``_collect_pipe_results`` gathers with
    ``return_exceptions=True``, so a cancellation arrives as an ordinary
    element here. Matching only ``Exception`` would skip it and let the
    pipeline report success.
    """
    for result in pipe_results:
        match result:
            case BrokenPipeError() | ConnectionResetError():
                continue
            case BaseException():
                raise result
