"""Build a line stream's ``CommandResult`` once its run has settled.

Part of the ``cuprum._line_stream`` package. The exit wait and the post-exit
drain belong to ``cuprum._line_stream.coordinator`` and
``cuprum._line_stream.drain``; what remains afterwards is assembly — closing
the delivery worker, emitting the exit event, and reading the settled captures
into the result. That tail is separable from the waiting because it touches no
task the run still owns, and it is kept here so the coordination module does
not have to carry it beside control flow it shares with ``run()``.

Nothing in this module is patched by tests; the coordinator's seams are
deliberately left where the coordinator resolves them.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._delivery_lifecycle import _close_delivery_worker
from cuprum._line_stream.captures import _finalize_line_stream_captures
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._result_assembly import _recorded_captures, _require_text
from cuprum._subprocess_execution import _relay_fallbacks_for_result
from cuprum._subprocess_timeout import (
    _emit_run_exit_event,
    _SubprocessTimeoutError,
    _translate_run_timeout,
)
from cuprum.line_stream_events import LineStreamPhase

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._stream_drain_finish import _CapturedDrain
    from cuprum.sh import CommandResult

__all__ = ["_assemble_line_stream_result"]


@dc.dataclass(frozen=True, slots=True)
class _SettledRun:
    """One run's exit facts and the two drain records it settled.

    Carried as a unit because the exit code and timestamp travel with the
    streams they were measured beside; splitting them into separate arguments
    would let a caller pair one run's exit with another's output.
    """

    exit_code: int
    exited_at: float
    stdout_drain: _CapturedDrain
    stderr_drain: _CapturedDrain


async def _assemble_line_stream_result(
    run: _LineStreamRun,
    wait: cabc.Callable[
        [_LineStreamRun],
        cabc.Awaitable[tuple[int, float, _CapturedDrain, _CapturedDrain]],
    ],
) -> CommandResult:
    """Settle a finished run's captures and build its ``CommandResult``.

    *wait* is the exit-and-drain step, injected rather than imported so this
    module stays free of the coordinator while the coordinator keeps the name
    its tests patch. It returns the exit code, exit timestamp, and the two
    drain records.

    Always text: ``lines()`` is a text-mode driver and has no byte-exact entry
    point, so the run reaching here has a text config. The drain still reports
    the widened payload its shared signature promises, so the text guarantee
    is re-taken rather than assumed.

    Returns
    -------
    CommandResult
        The completed run's exit code, captured text, and relay diagnostics.
    """
    execution = run.execution
    settled = await _await_settled_drains(run, wait)
    await _shielded_cleanup(
        _close_delivery_worker(
            execution.delivery_worker,
            drain_grace_s=execution.delivery_grace_s,
        ),
    )
    run.telemetry.emit(LineStreamPhase.COMPLETED)
    _emit_run_exit_event(
        execution,
        settled,
        pid=run.process.pid,
    )
    return _build_command_result(run, settled)


async def _await_settled_drains(
    run: _LineStreamRun,
    wait: cabc.Callable[
        [_LineStreamRun],
        cabc.Awaitable[tuple[int, float, _CapturedDrain, _CapturedDrain]],
    ],
) -> _SettledRun:
    """Run the exit-and-drain step and finalize the captures it returned.

    The capture finalization belongs with the drain that produced the records,
    not with the result built afterwards: it releases each sink the drain
    returned, and a result assembled from unreleased sinks would not be the one
    the caller is owed.

    Returns
    -------
    _SettledRun
        The exit facts and the two drain records, ready to be read off.
    """
    try:
        exit_code, exited_at, stdout_drain, stderr_drain = await wait(run)
    except _SubprocessTimeoutError as exc:
        # Only the internal wrapper is translated here. Every genuine expiry in
        # the exit helper has already been through ``_handle_stream_timeout``,
        # which raises that wrapper; a bare ``TimeoutError`` escaping it is a
        # reader's own failure — an ``on_line`` hook using ``asyncio.timeout``
        # internally, say — and must reach the caller unchanged rather than be
        # reported as this run's deadline.
        _translate_run_timeout(
            run.execution,
            run.process,
            (None, None),
            exc,
        )
    await _finalize_line_stream_captures(run, stdout_drain, stderr_drain)
    return _SettledRun(
        exit_code=exit_code,
        exited_at=exited_at,
        stdout_drain=stdout_drain,
        stderr_drain=stderr_drain,
    )


def _build_command_result(run: _LineStreamRun, settled: _SettledRun) -> CommandResult:
    """Read a settled run's drains into the public ``CommandResult``.

    This module's single return point for that value, so the fields are named
    once even though the wait that filled them is injected.

    Returns
    -------
    CommandResult
        The completed run's exit code, captured text, and relay diagnostics.
    """
    execution = run.execution
    from cuprum._subprocess_context import _sh_module

    return _sh_module().CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=settled.exit_code,
        pid=run.process.pid if run.process.pid is not None else -1,
        stdout=_require_text(settled.stdout_drain.payload, "stdout"),
        stderr=_require_text(settled.stderr_drain.payload, "stderr"),
        relay_fallbacks=_relay_fallbacks_for_result(run.tasks.relay_diagnostics),
        resolved_path=execution.observation.resolved_path,
        captures=_recorded_captures(
            (settled.stdout_drain.capture, settled.stderr_drain.capture),
        ),
    )
