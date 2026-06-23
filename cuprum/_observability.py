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
import types
import typing as typ

from cuprum.context import current_context, merge_env_overlays

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook
    from cuprum.sh import SafeCmd
import logging


def _merge_tags(*tags: cabc.Mapping[str, object] | None) -> cabc.Mapping[str, object]:
    """Merge tag mappings left-to-right into a single read-only mapping."""
    merged: dict[str, object] = {}
    for mapping in tags:
        if not mapping:
            continue
        merged.update(mapping)
    return types.MappingProxyType(merged)


def _resolve_env_overlay(
    extra: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Resolve the frozen observation env overlay for the active context."""
    return merge_env_overlays(current_context().env_overlay, extra)


def _base_stage_tags(
    cmd: SafeCmd,
    *,
    capture: bool,
    echo: bool,
) -> dict[str, object]:
    """Build the base observation tags for one command stage."""
    return {
        "project": cmd.project.name,
        "capture": capture,
        "echo": echo,
    }


def _emit_exec_event(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Invoke observe hooks and return any scheduled async-hook tasks.

    Synchronous hooks run inline. Hooks that return an awaitable are scheduled
    as background tasks; those tasks are returned so the caller can extend its
    own pending-task collection. Returns an empty list when no hook scheduled
    work.

    Raises
    ------
    _ExecEventEmissionError
        When a hook raises while handling ``event``. The exception carries any
        async-hook tasks scheduled before the failing hook so callers can keep
        awaiting them during cleanup.
    """
    scheduled: list[asyncio.Task[None]] = []
    for hook in hooks:
        try:
            result = hook(event)
        except asyncio.CancelledError as exc:
            raise _ExecEventEmissionError(exc, scheduled) from exc
        except Exception as exc:
            _LOGGER.warning(
                "observe_hook_failed phase=%s program=%s error=%s",
                event.phase,
                event.program,
                type(exc).__name__,
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={
                    "cuprum_phase": event.phase,
                    "cuprum_program": str(event.program),
                    "cuprum_error_type": type(exc).__name__,
                    "cuprum_scheduled_task_count": len(scheduled),
                },
            )
            raise _ExecEventEmissionError(exc, scheduled) from exc
        if inspect.isawaitable(result):
            scheduled.append(
                asyncio.create_task(
                    _await_awaitable(result, event.phase),
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
    return scheduled


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
        _LOGGER.warning(
            "observe_hook_task_failed phase=%s error=%s",
            phase,
            type(exc).__name__,
            exc_info=(type(exc), exc, exc.__traceback__),
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
            _LOGGER.warning(
                "observe_hook_pending_task_failed error=%s",
                type(result).__name__,
                exc_info=(type(result), result, result.__traceback__),
                extra={"cuprum_error_type": type(result).__name__},
            )
            raise result


__all__ = [
    "_ExecEventEmissionError",
    "_base_stage_tags",
    "_emit_exec_event",
    "_freeze_str_mapping",
    "_merge_tags",
    "_resolve_env_overlay",
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
