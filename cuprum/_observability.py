"""Internal helpers for structured execution event emission.

This module is the dependency-free home for the canonical stage-observation
inputs shared by the single-command and pipeline execution paths:
:func:`_resolve_env_overlay` and :func:`_base_stage_tags`. The observation tag
schema is a wire contract for observability, so it is computed in exactly one
place; the pipeline builders graft on only their stage-specific keys.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import types
import typing as typ

from cuprum.context import current_context
from cuprum.context._policy import _resolve_env_policy
from cuprum.context.env_overlay import EnvMode, EnvOverlay

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent, ExecHook
    from cuprum.sh import SafeCmd


_LOGGER = logging.getLogger(__name__)

# How many unfinished async observe-hook tasks one execution may retain before
# line observation applies backpressure. The value matches
# ``cuprum._line_stream._LINE_QUEUE_CAPACITY``: the two bounds describe the same
# quantity — how much asynchronous work one observed run may have in flight — so
# a slow observer cannot queue more work than the line driver already queues,
# and a chatty child is paused once either bound is reached. The retention list
# is shared by every stage of a pipeline, so the ceiling is per run rather than
# per stream.
_OBSERVE_TASK_CEILING = 256


def _merge_tags(*tags: cabc.Mapping[str, object] | None) -> cabc.Mapping[str, object]:
    """Merge tag mappings left-to-right into a single read-only mapping."""
    merged: dict[str, object] = {}
    for mapping in tags:
        if not mapping:
            continue
        merged.update(mapping)
    return types.MappingProxyType(merged)


def _without_env_mode_tag(
    tags: cabc.Mapping[str, object] | None,
) -> cabc.Mapping[str, object] | None:
    """Return caller tags without the reserved environment-mode key."""
    if tags is None or "env_mode" not in tags:
        return tags
    return {key: value for key, value in tags.items() if key != "env_mode"}


def _resolve_env_overlay(
    extra: EnvOverlay | None,
    env_mode: EnvMode = EnvMode.OVERLAY,
) -> tuple[EnvOverlay | None, EnvMode]:
    """Resolve the frozen observation environment policy for the active context.

    Returns
    -------
    tuple[EnvOverlay | None, EnvMode]
        The composed overlay and mode, without reading ``os.environ``.
    """
    context = current_context()
    return _resolve_env_policy(
        context.env_overlay,
        context.env_mode,
        extra,
        env_mode,
    )


def _resolve_executable_for(cmd: SafeCmd, *, cwd: Path | None) -> str | None:
    """Return the executable bound to ``cmd``'s program, or ``None``.

    The third of the stage-observation inputs this module owns, alongside
    :func:`_resolve_env_overlay` and :func:`_base_stage_tags`: each answers a
    question the observation carries the answer to, so they are computed where
    a reader of the observation will look for them.

    ``cwd`` is the same ``Path`` the observation holds rather than a string,
    so callers hand over what they already have and the one conversion to the
    public method's spelling lives here.

    Call this strictly *after* allowlist enforcement, never before: the
    allowlist decides whether the program may run at all, and this decides only
    which file a permitted program runs. The two are independent by design, so
    a binding can never admit a program the allowlist rejects — and a resolver
    with side effects is not invoked for a command that is about to be refused.

    Returns
    -------
    str | None
        The executable string, or ``None`` when the program is unbound and
        should run under its catalogued name.
    """
    return current_context().resolve_executable(
        cmd.program, cwd=None if cwd is None else str(cwd)
    )


def _base_stage_tags(
    cmd: SafeCmd,
    *,
    capture: bool,
    echo_stdout: bool,
    echo_stderr: bool,
) -> dict[str, object]:
    """Build the base observation tags for one command stage.

    The capture *mode* is deliberately absent, unlike ``capture`` itself: it
    travels as the trusted ``capture_mode`` field on the event, for the reason
    ``env_mode`` does. A caller may shadow any tag key, and the adapters project
    the typed field rather than reading tags, so a tag here would add a second,
    forgeable spelling of a fact that already has a reliable one.

    Returns
    -------
    dict[str, object]
        The project, capture, and echo tags every stage starts from.
    """
    return {
        "project": cmd.project.name,
        "capture": capture,
        "echo": echo_stdout or echo_stderr,
        "echo_stdout": echo_stdout,
        "echo_stderr": echo_stderr,
    }


def _emit_exec_event(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Invoke observe hooks and return any scheduled async-hook tasks.

    Synchronous hooks run inline. Hooks that return an awaitable are scheduled
    as background tasks; those tasks are returned so the caller can extend its
    own pending-task collection. A failing ``settled`` hook does not prevent
    later hooks from observing the terminal event, because adapters must all
    release per-execution state before the first failure is surfaced.

    Returns
    -------
    list[asyncio.Task[None]]
        The async-hook tasks scheduled while emitting ``event``; an empty list
        when no hook scheduled work.

    """
    scheduled: list[asyncio.Task[None]] = []
    if event.phase == "settled":
        return _emit_settled_hooks(hooks, event, scheduled)
    for hook in hooks:
        _emit_observe_hook(hook, event, scheduled)
    return scheduled


def _emit_settled_hooks(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
    scheduled: list[asyncio.Task[None]],
) -> list[asyncio.Task[None]]:
    """Try every terminal hook before surfacing the first synchronous failure."""
    first_error: BaseException | None = None
    for hook in hooks:
        try:
            _emit_observe_hook(hook, event, scheduled)
        except _ExecEventEmissionError as exc:
            if first_error is None:
                first_error = exc.error
    if first_error is not None:
        raise _ExecEventEmissionError(first_error, scheduled) from first_error
    return scheduled


def _emit_observe_hook(
    hook: ExecHook,
    event: ExecEvent,
    scheduled: list[asyncio.Task[None]],
) -> None:
    """Invoke one hook, retaining any async task for the caller to await."""
    try:
        result = hook(event)
    except asyncio.CancelledError as exc:
        raise _ExecEventEmissionError(exc, scheduled) from exc
    except BaseException as exc:
        _LOGGER.warning(
            "observe_hook_failed phase=%s program=%s error=%s",
            event.phase,
            event.program,
            type(exc).__name__,
            exc_info=True,
            extra={
                "cuprum_phase": event.phase,
                "cuprum_program": str(event.program),
                "cuprum_error_type": type(exc).__name__,
                "cuprum_scheduled_task_count": len(scheduled),
            },
        )
        raise _ExecEventEmissionError(exc, scheduled) from exc
    if inspect.isawaitable(result):
        _schedule_observe_hook_task(result, event, scheduled)


def _schedule_observe_hook_task(
    awaitable: cabc.Awaitable[None],
    event: ExecEvent,
    scheduled: list[asyncio.Task[None]],
) -> None:
    """Schedule one async hook result and record the task for cleanup."""
    scheduled.append(
        asyncio.create_task(
            _await_awaitable(awaitable, event.phase),
            name=f"cuprum.observe.{event.phase}",
        )
    )
    _LOGGER.debug(
        "observe_hook_task_scheduled phase=%s program=%s count=%s",
        event.phase,
        event.program,
        len(scheduled),
        extra={
            "cuprum_phase": event.phase,
            "cuprum_program": str(event.program),
            "cuprum_scheduled_task_count": len(scheduled),
        },
    )


async def _await_awaitable(
    awaitable: cabc.Awaitable[None],
    phase: str,
) -> None:
    """Await ``awaitable`` so it can be wrapped in a task."""
    _LOGGER.debug(
        "observe_hook_task_started phase=%s",
        phase,
        extra={"cuprum_phase": phase},
    )
    try:
        await awaitable
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _LOGGER.exception(
            "observe_hook_task_failed phase=%s error=%s",
            phase,
            type(exc).__name__,
            extra={
                "cuprum_phase": phase,
                "cuprum_error_type": type(exc).__name__,
            },
        )
        raise
    _LOGGER.debug(
        "observe_hook_task_finished phase=%s",
        phase,
        extra={"cuprum_phase": phase},
    )


def _prune_finished_hook_tasks(pending_tasks: list[asyncio.Task[None]]) -> None:
    """Drop settled hook tasks while preserving the failure that will surface.

    ``_wait_for_exec_hook_tasks`` reports the *first* failed or cancelled task
    in list order; every later failure is discarded unread. Once an execution
    has run long enough, retaining successful tasks (which can never fail) and
    later failures (which can never be reported) buys nothing but memory. This
    helper keeps only what finalization can still use: every unfinished task,
    plus the earliest settled failure, exactly the task the gather would have
    raised.

    The list is mutated in place because it is shared by every stage of a
    pipeline; rebinding it would leave other stages retaining into a list that
    finalization never drains.

    Exceptions are retrieved from every dropped failed task before it is
    removed. A task destroyed with an unretrieved exception logs "Task
    exception was never retrieved" at garbage-collection time, which would
    blame the drop for a failure that was never going to be reported.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list, pruned in place.

    """
    kept: list[asyncio.Task[None]] = []
    retained_failure = False
    for task in pending_tasks:
        if not task.done():
            kept.append(task)
            continue
        # ``cancelled()`` must be checked first: calling ``exception()`` on a
        # cancelled task raises the ``CancelledError`` it was cancelled with.
        failed = task.cancelled() or task.exception() is not None
        if failed and not retained_failure:
            retained_failure = True
            kept.append(task)
    pending_tasks[:] = kept


def _has_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
) -> bool:
    """Return whether one more hook task may be scheduled right now.

    The fast path counts the whole list rather than scanning for unfinished
    tasks: below the ceiling the extra work would be pure overhead, and the
    list only ever holds settled tasks long enough to cross that threshold.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place when the raw
        length has reached ``ceiling``.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.

    Returns
    -------
    bool
        ``True`` when the number of unfinished tasks is below ``ceiling``.

    """
    if len(pending_tasks) < ceiling:
        return True
    _prune_finished_hook_tasks(pending_tasks)
    return sum(1 for task in pending_tasks if not task.done()) < ceiling


async def _await_hook_capacity(
    pending_tasks: list[asyncio.Task[None]],
    ceiling: int,
) -> None:
    """Wait until a hook task may be scheduled, then return.

    This is the backpressure primitive for line observation. Waiting on
    :func:`asyncio.wait` with ``FIRST_COMPLETED`` rather than gathering the
    pending tasks matters for two reasons: gathering would also await the
    remaining tasks (holding the caller far longer than a slot needs), and
    cancellation of this waiter releases the wait without touching the
    observer tasks themselves. Those tasks belong to the execution and are
    settled by its final drain, so a cancelled line driver must not be able to
    cancel them out from under it.

    Parameters
    ----------
    pending_tasks:
        The execution's shared retention list; pruned in place.
    ceiling:
        Maximum number of unfinished hook tasks to retain; must be positive.

    """
    while not _has_hook_capacity(pending_tasks, ceiling):
        # ``_has_hook_capacity`` returned ``False``, so at least ``ceiling``
        # (and therefore at least one) unfinished task remains: the list
        # passed to ``asyncio.wait`` is never empty.
        unfinished = [task for task in pending_tasks if not task.done()]
        _ = await asyncio.wait(unfinished, return_when=asyncio.FIRST_COMPLETED)


async def _wait_for_exec_hook_tasks(pending_tasks: list[asyncio.Task[None]]) -> None:
    """Await background observe-hook tasks and surface the first failure.

    Observe hooks may return awaitables; those awaitables are scheduled as tasks
    by ``_emit_exec_event``, whose return value the caller extends onto its
    ``pending_tasks`` collection. This helper awaits all pending tasks and
    re-raises the first ``BaseException`` encountered.

    Notes
    -----
    When multiple hooks fail, only the first exception is raised; subsequent
    exceptions are not surfaced and may be masked by the first failure.

    """
    if not pending_tasks:
        return
    results = await asyncio.gather(*pending_tasks, return_exceptions=True)
    pending_tasks.clear()
    for result in results:
        if isinstance(result, BaseException):
            raise result


async def _drain_tasks_during_cleanup(
    pending_tasks: list[asyncio.Task[None]],
    active_error: BaseException,
    *,
    message: str,
) -> None:
    """Drain observe tasks in cleanup, aggregating a failure with ``active_error``.

    Shared by the pipeline and single-command cleanup paths. ``message`` is
    required rather than defaulted so a caller cannot silently inherit another
    path's finalization label.

    Raises
    ------
    BaseExceptionGroup
        When a drained task fails, pairing that failure with ``active_error``
        under ``message`` so cleanup never masks the error that triggered it.
    """
    try:
        await _wait_for_exec_hook_tasks(pending_tasks)
    # Catch every task failure, including non-Exception BaseExceptions, so
    # cleanup cannot mask the error that triggered it.
    except BaseException as task_error:  # ruff: ignore[blind-except]
        raise BaseExceptionGroup(
            message,
            (active_error, task_error),
        ) from None


__all__ = [
    "_OBSERVE_TASK_CEILING",
    "_ExecEventEmissionError",
    "_await_hook_capacity",
    "_base_stage_tags",
    "_drain_tasks_during_cleanup",
    "_emit_exec_event",
    "_has_hook_capacity",
    "_merge_tags",
    "_prune_finished_hook_tasks",
    "_resolve_env_overlay",
    "_resolve_executable_for",
    "_wait_for_exec_hook_tasks",
]


class _ExecEventEmissionError(Exception):
    """Carry scheduled observe-hook tasks when later hook emission fails."""

    def __init__(
        self,
        error: BaseException,
        scheduled_tasks: list[asyncio.Task[None]],
    ) -> None:
        """Store ``error`` and tasks scheduled before it was raised."""
        super().__init__(str(error))
        self.error = error
        self.scheduled_tasks = scheduled_tasks
