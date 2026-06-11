"""Internal helpers for structured execution event emission."""

from __future__ import annotations

import asyncio
import inspect
import types
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


def _freeze_str_mapping(
    mapping: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Return a read-only copy of ``mapping``, or ``None`` when absent."""
    if mapping is None:
        return None
    return types.MappingProxyType(dict(mapping))


def _merge_tags(*tags: cabc.Mapping[str, object] | None) -> cabc.Mapping[str, object]:
    """Merge tag mappings left-to-right into a single read-only mapping."""
    merged: dict[str, object] = {}
    for mapping in tags:
        if not mapping:
            continue
        merged.update(mapping)
    return types.MappingProxyType(merged)


def _emit_exec_event(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Invoke observe hooks and return any scheduled async-hook tasks.

    Synchronous hooks run inline. Hooks that return an awaitable are scheduled
    as background tasks; those tasks are returned so the caller can extend its
    own pending-task collection. Returns an empty list when no hook scheduled
    work.
    """
    scheduled: list[asyncio.Task[None]] = []
    for hook in hooks:
        result = hook(event)
        if inspect.isawaitable(result):
            scheduled.append(asyncio.create_task(_await_awaitable(result)))
    return scheduled


async def _await_awaitable(awaitable: cabc.Awaitable[None]) -> None:
    """Await ``awaitable`` so it can be wrapped in a task."""
    await awaitable


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


__all__ = [
    "_emit_exec_event",
    "_freeze_str_mapping",
    "_merge_tags",
    "_wait_for_exec_hook_tasks",
]
