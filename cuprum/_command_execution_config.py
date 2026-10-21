"""Build the resolved execution bundle for one safe command."""

from __future__ import annotations

import functools
import sys
import typing as typ

from cuprum._bounded_delivery import _DeliveryWorker, _new_delivery_worker
from cuprum._bounded_encoding import _reject_unsupported_bounded_captures
from cuprum._delivery_lifecycle import _queue_idle_diagnostic
from cuprum._idle_diagnostic import _idle_subject
from cuprum._idle_heartbeat import _build_idle_monitor, _IdleSinkConfig
from cuprum._stdio_plan import _resolve_stdio
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum._subprocess_execution import _SubprocessExecution
from cuprum.sh.stdio_rules import _reject_contested_stdin

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._command_internals import _ExecutionState
    from cuprum._idle_heartbeat import _IdleMonitor
    from cuprum._pipeline_types import _StageObservation
    from cuprum._sink_lifecycle import _SinkBracket
    from cuprum.sh import SafeCmd


def _resolve_command_destinations(
    state: _ExecutionState,
    sink_bracket: _SinkBracket,
) -> tuple[typ.IO[str], typ.IO[str]]:
    """Resolve the command's stdout and stderr sinks through its bracket."""
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
    return (
        sink_bracket.resolve_destination(stdout_fallback),
        sink_bracket.resolve_destination(stderr_fallback),
    )


def _reject_contested_captures(state: _ExecutionState) -> None:
    """Refuse a bounded capture the run's encoding cannot safely trim.

    A bounded capture trimmed under an encoding the trim cannot repair would
    decode an orphaned trail byte as a character of its own, and the caller
    would read text the child never wrote. Both facts — the policy and the
    encoding — are in hand here and nowhere earlier: the policy lives on the
    options and the encoding on the context, and this seam is the first that
    brings them together.

    Raises
    ------
    ValueError
        If either stream names a ``BoundedCapture`` whose encoding is not
        trim-safe and the run renders text.
    """  # ruff: ignore[docstring-extraneous-exception] - raised by the guard this delegates to
    _reject_unsupported_bounded_captures(
        state.output.resolved_capture,
        encoding=state.context.encoding,
        capture_bytes=state.capture_bytes,
    )


def _build_subprocess_execution(
    cmd: SafeCmd,
    state: _ExecutionState,
    *,
    observation: _StageObservation,
    sink_bracket: _SinkBracket,
) -> _SubprocessExecution:
    """Bundle everything one command's execution needs, before it spawns."""
    stdout_destination, stderr_destination = _resolve_command_destinations(
        state,
        sink_bracket,
    )
    _reject_contested_stdin(
        state.output.stdin,
        has_source=state.stdin_data is not None,
    )
    _reject_contested_captures(state)
    capture_stdout, capture_stderr = state.output.resolved_capture
    delivery = state.output.delivery
    delivery_worker = (
        None
        if delivery is None
        else _new_delivery_worker(
            delivery,
            stdout_destination,
            stderr_destination,
            operation_lock=sink_bracket.writer_lock,
        )
    )
    try:
        relay_diagnostics, idle_delivery = _build_delivery_diagnostics(delivery_worker)
        return _SubprocessExecution(
            cmd=cmd,
            ctx=state.context,
            capture_stdout=capture_stdout,
            capture_stderr=capture_stderr,
            echo_stdout=state.output.resolved_echo[0],
            echo_stderr=state.output.resolved_echo[1],
            max_echo_line_bytes=state.output.max_echo_line_bytes,
            max_line_chars=state.output.max_line_chars,
            broken_pipe_policy=state.output.resolved_broken_pipe_policy,
            sink_session=sink_bracket.session,
            timeout=state.timeout,
            observation=observation,
            stdio=_resolve_stdio(state.stdin_data, state.output),
            on_line=state.output.on_line,
            capture_bytes=state.capture_bytes,
            process_group=state.context.resolved_process_group,
            idle=_build_command_idle_monitor(
                cmd,
                state,
                stderr_destination,
                idle_delivery,
            ),
            delivery_worker=delivery_worker,
            delivery_grace_s=None if delivery is None else delivery.drain_grace_s,
            relay_diagnostics=relay_diagnostics,
        )
    except BaseException as run_error:
        if delivery_worker is not None:
            try:
                delivery_worker.close(grace=0.0)
            except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the setup error.
                run_error.add_note(
                    "Output delivery worker cleanup also failed "
                    f"({type(close_error).__name__})."
                )
        raise


def _build_delivery_diagnostics(
    worker: _DeliveryWorker | None,
) -> tuple[
    tuple[_RelayDiagnostics, _RelayDiagnostics] | None,
    cabc.Callable[[str], None] | None,
]:
    """Build stream diagnostics and the optional stderr heartbeat route."""
    if worker is None:
        return None, None
    diagnostics = (_RelayDiagnostics(), _RelayDiagnostics())
    idle_delivery = functools.partial(
        _queue_idle_diagnostic,
        worker,
        diagnostics[1],
    )
    return diagnostics, idle_delivery


def _build_command_idle_monitor(
    cmd: SafeCmd,
    state: _ExecutionState,
    stderr_destination: typ.IO[str],
    idle_delivery: cabc.Callable[[str], None] | None,
) -> _IdleMonitor | None:
    """Build the command heartbeat with its resolved stderr destination."""
    return _build_idle_monitor(
        state.output.idle_after,
        state.output.on_idle,
        _idle_subject(str(cmd.program)),
        _IdleSinkConfig(sink=stderr_destination, delivery=idle_delivery),
    )


__all__ = ["_build_subprocess_execution"]
