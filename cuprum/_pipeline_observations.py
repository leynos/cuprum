"""Per-stage observation state for a pipeline run.

``_build_pipeline_observations`` assembles one ``_StageObservation`` per
command: it enforces the active context's allowlist, collects the hooks
registered on that context, resolves the working directory and environment
overlay, and merges the base stage tags with the pipeline's own stage index and
stage count. Tag construction itself belongs to ``cuprum._observability``; the
decision of *which* tags a pipeline stage carries is what lives here.

``_emit_plan_events_and_run_before_hooks`` then fires the ``plan`` event and the
registered before hooks for every stage, in stage order, before any process is
spawned. The two halves are kept apart deliberately: ``_enforce_allowlist`` and
``_collect_hooks`` are pure queries with no emission or mutation, which is what
lets ``cuprum._command_internals`` reuse both on the single-command path
without inheriting the pipeline's event traffic.

This module is called by ``cuprum._pipeline_internals`` (which owns the spawn
and finalization half of ``Pipeline.run``), by ``cuprum._pipeline_spawn`` when
a helper spawns stages without an observation build of its own, and by
``cuprum._command_internals`` and ``cuprum.sh`` on the command path. It
collaborates with ``cuprum._observability``, ``cuprum._pipeline_config``,
``cuprum._pipeline_types``, and ``cuprum.context``.
"""

from __future__ import annotations

import time
import typing as typ
from pathlib import Path

from cuprum._observability import (
    _base_stage_tags,
    _merge_tags,
    _resolve_env_overlay,
    _without_env_mode_tag,
)
from cuprum._pipeline_types import (
    _EventDetails,
    _ExecutionHooks,
    _StageObservation,
)
from cuprum.context import EnvMode, current_context

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_config import _PipelineRunConfig
    from cuprum.context import CuprumContext
    from cuprum.sh import SafeCmd

__all__ = [
    "_build_pipeline_observations",
    "_collect_hooks",
    "_emit_plan_events_and_run_before_hooks",
    "_enforce_allowlist",
]


def _enforce_allowlist(cmd: SafeCmd) -> None:
    """Reject ``cmd`` when the active context forbids its program."""
    current_context().check_allowed(cmd.program)


def _collect_hooks(ctx: CuprumContext) -> _ExecutionHooks:
    """Return the before/after/observe hooks registered on ``ctx``."""
    return _ExecutionHooks(
        before_hooks=ctx.before_hooks,
        after_hooks=ctx.after_hooks,
        observe_hooks=ctx.observe_hooks,
    )


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
        )
        for idx, (cmd, hooks) in enumerate(zip(parts, hooks_by_stage, strict=True))
    )


def _emit_plan_events_and_run_before_hooks(
    observations: tuple[_StageObservation, ...],
) -> None:
    """Emit plan events and run before hooks for every stage."""
    for obs in observations:
        obs.emit("plan", _EventDetails(pid=None))
        for hook in obs.hooks.before_hooks:
            hook(obs.cmd)
