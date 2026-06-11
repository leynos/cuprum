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


def _resolve_env_overlay(
    extra: cabc.Mapping[str, str] | None,
) -> cabc.Mapping[str, str] | None:
    """Resolve the effective observation env overlay for the active context.

    This is the canonical computation shared by the single-command and
    pipeline observation builders: the per-call overlay (typically
    ``ExecutionContext.env``) is layered over the scoped overlay from the
    active :class:`~cuprum.context.CuprumContext`, and the result is frozen.
    The overlay stays overlay-only — ``os.environ`` is never included here;
    the live environment is merged separately at spawn time by
    :func:`cuprum.context.resolve_env`.

    Example
    -------
    >>> _resolve_env_overlay(None) is None  # no scoped or per-call overlay
    True
    """
    return _freeze_str_mapping(
        merge_env_overlays(current_context().env_overlay, extra),
    )


def _base_stage_tags(
    cmd: SafeCmd,
    *,
    capture: bool,
    echo: bool,
) -> dict[str, object]:
    """Build the canonical base observation tags for one command stage.

    This is the single source of truth for the shared tag schema
    (``project``, ``capture``, ``echo``). The pipeline observation builder
    grafts on only its stage-specific keys (``pipeline_stage_index``,
    ``pipeline_stages``); per-call tags are merged over the base by callers
    via :func:`_merge_tags`.

    Example
    -------
    >>> _base_stage_tags(cmd, capture=True, echo=False)  # doctest: +SKIP
    {'project': 'core-ops', 'capture': True, 'echo': False}
    """
    return {
        "project": cmd.project.name,
        "capture": capture,
        "echo": echo,
    }


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
    """Await ``awaitable`` so it can be wrapped in a task."""
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
    "_base_stage_tags",
    "_emit_exec_event",
    "_freeze_str_mapping",
    "_merge_tags",
    "_resolve_env_overlay",
    "_wait_for_exec_hook_tasks",
]
