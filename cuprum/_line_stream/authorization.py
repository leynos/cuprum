"""Re-authorize a prepared line stream in the context that consumes it.

``SafeCmd.lines()`` returns a lazy stream: it resolves the allowlist, the
timeout, the executable binding, the environment policy, and the hook set, and
only starts the subprocess when the first event is requested. Those two moments
can fall in different scopes, and a prepared stream is not permission to run —
so every decision an enclosing scope made on the caller's behalf is made again
here, in the task that pulls the first event.

This module owns that repeat. It also owns the cleanup a *failed* repeat needs:
the execution bundle may already have started the bounded output-delivery
worker, and a denied run must not leave that daemon thread parked.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

# Runtime imports, not ``TYPE_CHECKING`` ones: all are called here. None of
# these modules imports ``cuprum._line_stream``, ``cuprum.sh``, or
# ``cuprum._line_iteration``, so this cannot close a cycle.
from cuprum._context_policy import _collect_hooks, _enforce_allowlist
from cuprum._observability import (
    _merge_tags,
    _resolve_env_overlay,
    _resolve_executable_for,
    _without_env_mode_tag,
)
from cuprum._subprocess_context import _execution_cwd, _resolve_timeout
from cuprum.context import EnvMode, current_context

if typ.TYPE_CHECKING:
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution

__all__ = ["_refresh_line_stream_execution"]


def _refresh_line_stream_execution(
    execution: _SubprocessExecution,
    requested_timeout: float | None,
) -> _SubprocessExecution:
    """Re-authorize a pending pull and re-resolve every scope-derived input.

    Called from the drive generator's body, which runs in the context of the
    task awaiting the stream. The allowlist check therefore applies the scope
    in force *now*, not the one that existed when ``lines()`` was called; a
    program that has since been forbidden raises
    :class:`~cuprum.context.ForbiddenProgramError` here, before a queue, a
    task, a plan event, or a hook exists.

    Everything the preparing scope decided on the caller's behalf is decided
    again in the consuming one, because a scope can change more than whether a
    program may run:

    - The deadline is resolved with the same precedence ``run()`` applies, but
      the ambient term is read here rather than at construction: an explicit
      *requested_timeout* (including ``0``) wins, then the per-call
      ``ExecutionContext.timeout``, then the consuming scope's timeout.
      Passing ``None`` deliberately falls through to those fallbacks rather
      than disabling timeouts.
    - The executable binding is resolved here, so a scope that permits the
      same program but binds it to a different file runs *that* file. The
      preparation-time path would otherwise reach the spawn as ``argv[0]``,
      executing outside the consuming scope's binding.
    - The environment overlay and mode are resolved here, so the events this
      run emits describe the policy it actually ran under. A child's real
      environment is already rendered from the consuming scope at the spawn;
      without this the ``plan``, line, and terminal events would report the
      preparing scope's policy instead.

    Returns
    -------
    _SubprocessExecution
        A copy of *execution* carrying the freshly resolved timeout and
        observation. The input is unchanged, matching every other refresh of
        this frozen bundle.

    Raises
    ------
    ForbiddenProgramError
        If the active context's allowlist forbids ``execution.cmd.program``.
    ExecutableResolutionError
        If the consuming scope binds the program to a resolver that fails.
    """  # ruff: ignore[docstring-extraneous-exception] - both propagate from the checks this repeats
    try:
        _enforce_allowlist(execution.cmd)
        effective_timeout = _resolve_timeout(
            timeout=requested_timeout,
            context=execution.ctx,
        )
        observation = _refresh_stage_observation(execution)
    except BaseException as refusal:
        # The bundle may already own a delivery thread: preparing the
        # execution builds one, and nothing has been handed to a coordinator
        # that could close it on this path.
        _close_refused_delivery_worker(execution, refusal)
        raise
    return dc.replace(
        execution,
        timeout=effective_timeout,
        observation=observation,
    )


def _refresh_stage_observation(
    execution: _SubprocessExecution,
) -> _StageObservation:
    """Re-resolve the executable, environment policy, and hooks for a pull.

    Split from :func:`_refresh_line_stream_execution` because it answers one
    question — *which executable, under which environment policy and hook set*
    — while its caller owns the allowlist gate and the failure cleanup. It
    runs strictly after the allowlist check, so a resolver with side effects is
    never invoked for a program about to be refused.

    The hooks are re-collected here for the same reason as the binding: the
    preparing scope's hooks are not the consuming scope's hooks. Line
    iteration reads ``before_hooks``/``after_hooks`` and the observe events
    from this one object, so refreshing it keeps the hooks that run and the
    events that are emitted agreeing with each other.

    Call this before the observation has emitted anything. ``dc.replace``
    rebuilds the whole value, so the ``init=False`` bookkeeping fields —
    ``_plan_emitted``, ``_terminal_emitted``, and ``_started_pid`` — start over
    at their defaults; refreshing after a ``plan`` event would let the
    terminal event be emitted twice.

    Returns
    -------
    _StageObservation
        A copy of the prepared observation carrying the consuming scope's
        binding, environment policy, and hooks.
    """
    observation = execution.observation
    env_overlay, env_mode = _resolve_env_overlay(
        execution.ctx.env,
        execution.ctx.env_mode,
    )
    return dc.replace(
        observation,
        hooks=_collect_hooks(current_context()),
        env_overlay=env_overlay,
        env_mode=env_mode,
        resolved_path=_resolve_executable_for(
            execution.cmd,
            cwd=_execution_cwd(execution.ctx.cwd),
        ),
        # The preparation wrote the policy into the tags as well, and only for
        # a replacement boundary. Strip whatever it wrote — the caller's own
        # ``env_mode`` tag was already removed there — and restate it for the
        # mode now in force.
        tags=_merge_tags(
            _without_env_mode_tag(observation.tags),
            {"env_mode": env_mode} if env_mode is EnvMode.REPLACE else None,
        ),
    )


def _close_refused_delivery_worker(
    execution: _SubprocessExecution,
    refusal: BaseException,
) -> None:
    """Stop a refused run's delivery worker, noting any close failure on it."""
    worker = execution.delivery_worker
    if worker is None:
        return
    try:
        worker.close(grace=0.0)
    except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the refusal.
        refusal.add_note(
            f"Output delivery worker cleanup also failed "
            f"({type(close_error).__name__})."
        )
