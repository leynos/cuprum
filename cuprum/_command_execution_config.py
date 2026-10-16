"""Build the resolved execution bundle for one safe command."""

from __future__ import annotations

import functools
import sys
import typing as typ

from cuprum._bounded_delivery import _DeliveryWorker
from cuprum._delivery_lifecycle import _queue_idle_diagnostic
from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor, _IdleSinkConfig
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum._stdio_plan import _resolve_stdio
from cuprum.echo_events import EchoStream
from cuprum.sh.stdio_rules import _reject_contested_stdin

if typ.TYPE_CHECKING:
    from cuprum._command_internals import _ExecutionState
    from cuprum._pipeline_types import _StageObservation
    from cuprum._sink_lifecycle import _SinkBracket
    from cuprum.sh import SafeCmd


def _build_subprocess_execution(
    cmd: SafeCmd,
    state: _ExecutionState,
    *,
    observation: _StageObservation,
    sink_bracket: _SinkBracket,
) -> _SubprocessExecution:
    """Bundle everything one command's execution needs, before it spawns."""
    stdout_fallback = (
        state.context.stdout_sink
        if state.context.stdout_sink is not None
        else sys.stdout
    )
    stderr_fallback = (
        state.context.stderr_sink
        if state.context.stderr_sink is not None
        else sys.stderr
    )
    stdout_destination = sink_bracket.resolve_destination(stdout_fallback)
    stderr_destination = sink_bracket.resolve_destination(stderr_fallback)
    _reject_contested_stdin(
        state.output.stdin,
        has_source=state.stdin_data is not None,
    )
    delivery = state.output.delivery
    delivery_worker = (
        None
        if delivery is None
        else _DeliveryWorker(
            destinations={
                EchoStream.STDOUT: stdout_destination,
                EchoStream.STDERR: stderr_destination,
            },
            max_queued_bytes=delivery.max_queued_bytes,
        )
    )
    try:
        relay_diagnostics = (
            None if delivery_worker is None else (_RelayDiagnostics(), _RelayDiagnostics())
        )
        idle_delivery = (
            None
            if delivery_worker is None or relay_diagnostics is None
            else functools.partial(
                _queue_idle_diagnostic,
                delivery_worker,
                relay_diagnostics[1],
            )
        )
        return _SubprocessExecution(
            cmd=cmd,
            ctx=state.context,
            capture=state.output.capture,
            echo_stdout=state.output.resolved_echo[0],
            echo_stderr=state.output.resolved_echo[1],
            max_echo_line_bytes=state.output.max_echo_line_bytes,
            broken_pipe_policy=state.output.resolved_broken_pipe_policy,
            sink_session=sink_bracket.session,
            timeout=state.timeout,
            observation=observation,
            stdio=_resolve_stdio(state.stdin_data, state.output),
            on_line=state.output.on_line,
            capture_bytes=state.capture_bytes,
            process_group=state.context.resolved_process_group,
            idle=_build_idle_monitor(
                state.output.idle_after,
                state.output.on_idle,
                _idle_subject(str(cmd.program)),
                _IdleSinkConfig(
                    sink=stderr_destination,
                    delivery=idle_delivery,
                ),
            ),
            delivery_worker=delivery_worker,
            delivery_grace_s=None if delivery is None else delivery.drain_grace_s,
            relay_diagnostics=relay_diagnostics,
        )
    except BaseException as run_error:
        if delivery_worker is not None:
            try:
                delivery_worker.close()
            except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the setup error.
                run_error.add_note(
                    "Output delivery worker cleanup also failed "
                    f"({type(close_error).__name__})."
                )
        raise

__all__ = ["_build_subprocess_execution"]
