"""Policy reads shared by the single-command and pipeline execution paths.

Three questions are asked at every spawn site, and all are answered from the
active :class:`~cuprum.context.CuprumContext` alone:

- :func:`_enforce_allowlist` — the command, the decision. It *is* the allowlist
  gate; nothing may execute before it returns.
- :func:`_collect_hooks` — the context, the value. It copies the registered
  hooks into the immutable bundle the execution machinery carries.
- :func:`_merge_env` — the environment, the child's own. It resolves the
  ambient overlay against the per-call one and renders the result.

They live here rather than beside either caller because both the direct path
(``cuprum._command_internals``, ``cuprum._subprocess_spawn``) and the pipeline
path (``cuprum._pipeline_internals``, ``cuprum._pipeline_spawn``) need them,
and a helper owned by one would make the other import its sibling's module for
a policy read that belongs to neither. The module depends only on
``cuprum.context`` and ``cuprum._pipeline_types``, so it cannot participate in
an execution cycle.

The fourth such read, :func:`cuprum._observability._resolve_executable_for`,
lives with the other stage-observation inputs instead: it produces a field of
the observation rather than enforcing a policy.
"""

from __future__ import annotations

import typing as typ

from cuprum._pipeline_types import _ExecutionHooks
from cuprum.context import current_context
from cuprum.context._policy import _resolve_env_policy
from cuprum.context.env_overlay import EnvMode, EnvOverlay, render_env

if typ.TYPE_CHECKING:
    from cuprum.context import CuprumContext
    from cuprum.sh import SafeCmd

__all__ = [
    "_collect_hooks",
    "_enforce_allowlist",
    "_merge_env",
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


def _merge_env(
    extra: EnvOverlay | None,
    env_mode: EnvMode = EnvMode.OVERLAY,
    *,
    include_context_overlay: bool = True,
) -> dict[str, str] | None:
    """Render the ambient and per-call environment policies for a child.

    Returns
    -------
    dict[str, str] | None
        The child's environment, or ``None`` to inherit the parent's unchanged.
    """
    context = current_context()
    parent_overlay = context.env_overlay if include_context_overlay else None
    parent_mode = context.env_mode if include_context_overlay else EnvMode.OVERLAY
    overlay, mode = _resolve_env_policy(
        parent_overlay,
        parent_mode,
        extra,
        env_mode,
    )
    return render_env(overlay, mode)
