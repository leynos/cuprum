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
    if mapping is None:
        return None
    return types.MappingProxyType(dict(mapping))


def _merge_tags(*tags: cabc.Mapping[str, object] | None) -> cabc.Mapping[str, object]:
    merged: dict[str, object] = {}
    for mapping in tags:
        if not mapping:
            continue
        merged.update(mapping)
    return types.MappingProxyType(merged)


def _emit_exec_event(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
    *,
    pending_tasks: list[asyncio.Task[None]],
) -> None:
    """Invoke observe hooks and schedule async hooks as background tasks."""
    for hook in hooks:
        result = hook(event)
        if inspect.isawaitable(result):
            pending_tasks.append(asyncio.create_task(_await_awaitable(result)))


async def _await_awaitable(awaitable: cabc.Awaitable[None]) -> None:
    await awaitable


async def _wait_for_exec_hook_tasks(pending_tasks: list[asyncio.Task[None]]) -> None:
    """Await background observe-hook tasks and surface the first failure.

    Observe hooks may return awaitables; those awaitables are scheduled as tasks
    by ``_emit_exec_event`` and added to ``pending_tasks``. This helper awaits
    all pending tasks and re-raises the first ``BaseException`` encountered.

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
