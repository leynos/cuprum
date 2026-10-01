"""Event setup for pipeline stages.

This module prepares the observation state used by pipeline execution. It keeps
authorization ahead of event creation, then builds the per-stage context and
emits each plan before dispatching its before-hooks.

The two policy reads themselves — ``_enforce_allowlist`` and
``_collect_hooks`` — live in :mod:`cuprum._context_policy`, because the direct
command path needs them too and a policy read owned by one execution path
would make the other import its sibling's module for something that belongs to
neither. This module imports them for its own use;
:mod:`cuprum._pipeline_internals` is where their re-export for existing
callers lives.
"""

from __future__ import annotations

import time
import typing as typ
from pathlib import Path

from cuprum._context_policy import _collect_hooks, _enforce_allowlist
from cuprum._observability import (
    _base_stage_tags,
    _merge_tags,
    _resolve_env_overlay,
    _resolve_executable_for,
    _without_env_mode_tag,
)
from cuprum._pipeline_types import _EventDetails, _StageObservation
from cuprum.context import EnvMode, current_context

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum.sh import SafeCmd


def _build_pipeline_observations(
    parts: tuple[SafeCmd, ...],
    config: _PipelineRunConfig,
    *,
    pending_tasks: list[asyncio.Task[None]],
) -> tuple[_StageObservation, ...]:
    """Build per-stage observation state for every command in the pipeline."""
    for cmd in parts:
        _enforce_allowlist(cmd)
    ctx = current_context()
    hooks_by_stage = tuple(_collect_hooks(ctx) for _ in parts)
    cwd = None if config.ctx.cwd is None else Path(config.ctx.cwd)
    env_overlay, env_mode = _resolve_env_overlay(config.ctx.env, config.ctx.env_mode)
    # Every stage is enforced above before any is resolved, so a pipeline that
    # is refused at stage three never runs a resolver for stages one and two.
    resolved_paths = tuple(_resolve_executable_for(cmd, cwd=cwd) for cmd in parts)
    return tuple(
        _StageObservation(
            cmd=cmd,
            hooks=hooks,
            tags=_merge_tags(
                _base_stage_tags(
                    cmd,
                    capture=config.capture,
                    echo_stdout=config.echo_stdout,
                    echo_stderr=config.echo_stderr,
                ),
                {
                    "pipeline_stage_index": idx,
                    "pipeline_stages": len(parts),
                },
                _without_env_mode_tag(config.ctx.tags),
                {"env_mode": env_mode} if env_mode is EnvMode.REPLACE else None,
            ),
            cwd=cwd,
            env_overlay=env_overlay,
            pending_tasks=pending_tasks,
            wall_clock=time.time,
            env_mode=env_mode,
            resolved_path=resolved_path,
        )
        for idx, (cmd, hooks, resolved_path) in enumerate(
            zip(parts, hooks_by_stage, resolved_paths, strict=True)
        )
    )


def _emit_plan_events_and_run_before_hooks(
    observations: tuple[_StageObservation, ...],
) -> None:
    """Emit plan events and run before hooks for every stage."""
    for obs in observations:
        obs.emit("plan", _EventDetails(pid=None))
        for hook in obs.hooks.before_hooks:
            hook(obs.cmd)
