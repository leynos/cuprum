"""Stream-consumer and inter-stage pipe task bookkeeping.

Splitting a pipeline's byte movement in two: ``cuprum._pipeline_streams`` owns
*how* bytes cross one hop — the backend choice and the raw-descriptor hand-off —
while this module owns the *tasks* that carry them. It creates one pump task per
adjacent stage pair, cancels the per-stage capture tasks on teardown, and
collects each pipe task's outcome, distinguishing the broken pipes a pipeline
expects from the failures it must surface.

The reuse policy is narrow. Further task-lifecycle concerns for a pipeline's
own streams belong here; this is not a general-purpose task utility, and a
different caller should be designed against its own requirements rather than
widening these.
"""

from __future__ import annotations

import asyncio

from cuprum._pipeline_streams import _pump_stream_dispatch


def _create_pipe_tasks(
    processes: list[asyncio.subprocess.Process],
) -> list[asyncio.Task[None]]:
    """Create streaming tasks between adjacent pipeline stages."""
    return [
        asyncio.create_task(
            _pump_stream_dispatch(
                processes[idx].stdout,
                processes[idx + 1].stdin,
            ),
        )
        for idx in range(len(processes) - 1)
    ]


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
    """Raise the first pipe-task failure that is not an expected broken pipe.

    ``BrokenPipeError`` and ``ConnectionResetError`` are expected when a
    downstream process exits early — ``head`` is the canonical case — and must
    not fail the pipeline. Everything else did stop the data moving and has to
    reach the caller.

    The check is against ``BaseException``, not ``Exception``. Since Python 3.8
    ``asyncio.CancelledError`` derives from ``BaseException``, so an
    ``Exception`` guard silently ignores a cancelled pump task and lets the
    pipeline report success for bytes that were never delivered.
    """
    for result in pipe_results:
        if isinstance(result, BaseException) and not isinstance(
            result,
            (BrokenPipeError, ConnectionResetError),
        ):
            raise result
