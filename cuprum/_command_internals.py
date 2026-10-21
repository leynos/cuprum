"""Private execution orchestration for ``SafeCmd.run`` and ``run_sync``.

Like :mod:`cuprum._pipeline_internals`, this module prepares a validated
command's observation, resolves its stdio plan, and hands the run on. Public
command types and pipeline orchestration remain in :mod:`cuprum.sh`.

Driving the spawned child and settling its observation — the after-hooks, the
sink bracket, the terminal event, and the observer tasks — lives in
:mod:`cuprum._command_finalization`, which split out when the stdio-resolution
fix pushed this module past the 400-line ceiling. The seam is the one the code
already drew: this module decides *what* a run is, that one owns everything
that happens once the child exists and on every exit path afterwards. The two
names callers reach through here are re-exported so the existing import sites
are unchanged.
"""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

from cuprum._command_execution_config import (
    _build_subprocess_execution as _build_subprocess_execution,
)

# The finalization helpers moved to ``cuprum._command_finalization`` when this
# module outgrew the line ceiling. They stay in this module's namespace because
# the callers that reach for them — the owned-handle start, the prepared-command
# path, and the settlement coordinator — all import them from here, and the
# tests that replace them patch them here.
from cuprum._command_finalization import (
    _execute_with_hooks as _execute_with_hooks,
)
from cuprum._command_finalization import (
    _finalize_command_run_failure as _finalize_command_run_failure,
)
from cuprum._command_finalization import (
    _finalize_command_run_success as _finalize_command_run_success,
)
from cuprum._command_finalization import (
    _finalize_prepared_command_failure as _finalize_prepared_command_failure,
)
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._observability import (
    _base_stage_tags,
    _merge_tags,
    _resolve_env_overlay,
    _resolve_executable_for,
    _without_env_mode_tag,
)
from cuprum._pipeline_types import (
    _StageObservation,
)
from cuprum._subprocess_context import _event_cwd, _execution_cwd
from cuprum.context import EnvMode
from cuprum.events import CaptureMode

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import (
        ExecutionContext,
        RunOutputOptions,
        SafeCmd,
        StdinStream,
    )

__all__ = [
    "_ExecutionState",
    "_ExecutionTracking",
    "_build_subprocess_execution",
    "_execute_with_hooks",
    "_finalize_prepared_command_failure",
    "_prepare_execution_observation",
    "_run_prepared_command",
]


@dc.dataclass(frozen=True, slots=True)
class _ExecutionState:
    """One run's already-resolved inputs, carried as a unit.

    Every field is resolved before the sink session opens — the allowlist is
    enforced, stdin is resolved against the context, and the timeout
    precedence is settled — so the bundle changes nothing about when those
    steps happen, only how many names the run's orchestration helpers have to
    take.

    The entries that start a run each build one and hand it on: ``SafeCmd``
    for the foreground, background, and owned-handle paths, and
    ``_spawn_owned_run`` for the handle's own start. Nothing in this module
    constructs it, which is why the orchestrators here take it by reference
    and never assemble a partial one.
    """

    context: ExecutionContext
    output: RunOutputOptions
    # A payload already resolved against the context's encoding, a producer the
    # writer pulls during the run, or ``None`` for the parent's own stdin.
    # ``SafeCmd.run``/``lines`` resolve the payload half before constructing
    # this bundle, so nothing downstream re-encodes a ``StdinInput``; the stdio
    # plan and the stream bindings are resolved from it, together with
    # ``output``'s targets, in ``_build_subprocess_execution``.
    stdin_data: bytes | StdinStream | None
    timeout: float | None
    # Whether captures are reported as bytes rather than decoded text, carried
    # to both the drain and the event's ``capture_mode``.
    capture_bytes: bool = False


def _prepare_execution_observation(
    cmd: SafeCmd,
    state: _ExecutionState,
    tracking: _ExecutionTracking,
) -> _StageObservation:
    """Prepare the observation context for command execution."""
    context = state.context
    output = state.output
    cwd = _event_cwd(context.cwd)
    env_overlay, env_mode = _resolve_env_overlay(context.env, context.env_mode)
    # Resolved here, after the caller's allowlist check, so the resolver runs
    # once for a command that is actually going to run.
    resolved_path = _resolve_executable_for(cmd, cwd=_execution_cwd(context.cwd))
    tags = _merge_tags(
        _base_stage_tags(
            cmd,
            capture=output.captures_any_stream,
            echo_stdout=output.resolved_echo[0],
            echo_stderr=output.resolved_echo[1],
        ),
        _without_env_mode_tag(context.tags),
        {"env_mode": env_mode} if env_mode is EnvMode.REPLACE else None,
    )
    return _StageObservation(
        cmd=cmd,
        hooks=tracking.execution_hooks,
        cwd=cwd,
        env_overlay=env_overlay,
        tags=tags,
        pending_tasks=tracking.pending_tasks,
        # Resolved here, not at the class-creation field default a patch misses.
        wall_clock=time.time,
        monotonic_clock=time.perf_counter,
        env_mode=env_mode,
        resolved_path=resolved_path,
        capture_mode=CaptureMode.BYTES if state.capture_bytes else CaptureMode.TEXT,
    )


async def _run_prepared_command(
    cmd: SafeCmd,
    state: _ExecutionState,
) -> _AnyCommandResult:
    """Run one validated command after its public inputs are resolved.

    ``SafeCmd.run`` remains the public entry point and keeps its signature; it
    resolves the allowlist, stdin, and the effective timeout, bundles them as
    *state*, and hands the bundle here. This helper takes that bundle instead
    of the four names it carries, which keeps the orchestration signature
    within CodeScene's argument limit and makes the bundle what the docstring
    on :class:`_ExecutionState` claims it is — one run's resolved inputs
    travelling as a unit — rather than a temporary built only to be unpacked.

    The preparation prefix is shared with ``SafeCmd.start()`` and lives in
    ``cuprum._command_prepare``, so the two entry points cannot drift apart.
    It owns the failure handling for everything it does; what remains here is
    executed under :func:`_execute_with_hooks`, which owns finalization for the
    run itself.

    Parameters
    ----------
    cmd : SafeCmd
        The validated command to run.
    state : _ExecutionState
        The run's already-resolved inputs.

    Returns
    -------
    CommandResult | BytesCommandResult
        The completed command's result, byte-exact when bytes were asked for.
    """
    # Imported here, not at module scope: ``cuprum._command_prepare`` borrows
    # helpers from this module, so a module-level import would close the cycle.
    from cuprum._command_prepare import _prepare_owned_command

    prepared = await _prepare_owned_command(cmd, state)
    return await _execute_with_hooks(
        cmd,
        prepared.execution,
        prepared.tracking,
    )
