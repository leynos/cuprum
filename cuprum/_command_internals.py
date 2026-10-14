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
import sys
import time
import typing as typ

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
from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor
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
from cuprum._stdio_plan import _resolve_stdio
from cuprum._subprocess_context import _event_cwd, _execution_cwd
from cuprum._subprocess_execution import (
    _SubprocessExecution,
)
from cuprum._subprocess_streams import _resolve_stream_sink
from cuprum.context import EnvMode
from cuprum.events import CaptureMode
from cuprum.sh.stdio_rules import _reject_contested_stdin

if typ.TYPE_CHECKING:
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._result_types import _AnyCommandResult
    from cuprum.sh import (
        ExecutionContext,
        RunOutputOptions,
        SafeCmd,
        StdinStream,
    )
    from cuprum.sinks import base as sinks

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


def _build_subprocess_execution(
    cmd: SafeCmd,
    state: _ExecutionState,
    *,
    observation: _StageObservation,
    sink_session: sinks.OutputSession | None = None,
) -> _SubprocessExecution:
    """Bundle everything one command's execution needs, before it spawns.

    The run's already-resolved inputs arrive together as *state*, so this
    helper stays a pure translation from what the run decided to what the
    subprocess layer consumes; *observation* and *sink_session* stay separate
    because they are the two products of the run's own preparation rather
    than inputs it was handed.

    The idle monitor is part of the bundle rather than an execution-time
    argument because its presence is what decides whether the child's stdout
    and stderr are piped for activity observation. Deferring it would leave
    the spawn unable to make that choice. The sink session travels with it
    for the same reason: stream wiring routes mirrored output through the
    session's log, so it has to be part of the bundle before the consumers
    are built. The process-group policy is read from the context into the
    bundle for the complementary reason: the spawn and the teardown must
    agree about whether the child's group belongs to this run, and a single
    bundle field is what makes that agreement structural.

    Stdio is resolved here rather than in the state, because this is the first
    point at which the run's *output options* and its *stdin source* are both
    in hand: ``_ExecutionState`` carries the source and the options but was
    built by ``SafeCmd.run`` before the idle monitor and the sink session
    existed, and a redirected stream has to be resolved against whether
    anything still needs to read it.

    Being the first point where both are in hand is also what makes this the
    place to check that they agree. ``RunOutputOptions`` validates its own
    targets when it is constructed, but whether the run *also* carries a stdin
    source is not known there — the source arrives on the run call, which is
    what built the state. Establishing that the two answer the same question
    once is cheap; discovering it from a caller who watched their explicit
    ``inherit()`` lose to a payload is not.

    Returns
    -------
    _SubprocessExecution
        The resolved execution bundle, ready for ``_execute_with_hooks``.

    Raises
    ------
    ValueError
        If the run's output options name an inherited stdin while its
        ``stdin=`` argument supplies a source, so the two disagree about where
        the child's input comes from.
    """  # ruff: ignore[docstring-extraneous-exception] - ValueError propagates from the stdio checker.
    _reject_contested_stdin(state.output.stdin, has_source=state.stdin_data is not None)
    return _SubprocessExecution(
        cmd=cmd,
        ctx=state.context,
        capture_stdout=state.output.resolved_capture[0],
        capture_stderr=state.output.resolved_capture[1],
        echo_stdout=state.output.resolved_echo[0],
        echo_stderr=state.output.resolved_echo[1],
        max_echo_line_bytes=state.output.max_echo_line_bytes,
        broken_pipe_policy=state.output.resolved_broken_pipe_policy,
        sink_session=sink_session,
        timeout=state.timeout,
        observation=observation,
        stdio=_resolve_stdio(state.stdin_data, state.output),
        on_line=state.output.on_line,
        capture_bytes=state.capture_bytes,
        # Built here, during the parent's own preparation, but armed by the run
        # itself, once the child is actually running: everything preceding the
        # spawn is the parent's work, and must not read as the child's silence.
        idle=_resolve_idle_monitor(cmd, state, sink_session),
        # Read from the context here so the bundle's spawn and teardown stages
        # share one decision instead of each consulting the context separately.
        process_group=state.context.resolved_process_group,
    )


def _resolve_idle_monitor(
    cmd: SafeCmd,
    state: _ExecutionState,
    sink_session: sinks.OutputSession | None,
) -> _IdleMonitor | None:
    """Build this run's idle monitor, resolved against the run's own streams.

    The heartbeat's keepalive is written to the same place the stderr drain
    writes: the sink session's log, then the context's stderr sink, then the
    process's own stderr. That is not a stylistic choice — the keepalive
    shares the drain's incomplete-line state, so writing it anywhere else
    would land outside the group an active session claims.

    Built during the parent's own preparation, before the child exists, so
    that the run owns the monitor by the time it spawns; it is *armed* only
    once the child is actually running, because everything preceding the spawn
    is the parent's work and must not read as the child's silence.

    Parameters
    ----------
    cmd : SafeCmd
        The command whose program names the idle subject.
    state : _ExecutionState
        The run's already-resolved inputs, carrying the idle settings and the
        context's stderr destination.
    sink_session : sinks.OutputSession | None
        The run's open sink session, or ``None`` when it has none.

    Returns
    -------
    _IdleMonitor | None
        The monitor to carry on the execution bundle, or ``None`` when the
        caller asked for no idle reporting.
    """
    return _build_idle_monitor(
        state.output.idle_after,
        state.output.on_idle,
        _idle_subject(str(cmd.program)),
        _resolve_stream_sink(sink_session, state.context.stderr_sink, sys.stderr),
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
