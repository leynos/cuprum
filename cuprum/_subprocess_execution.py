"""Internal subprocess execution machinery for ``SafeCmd.run()``.

Orchestration for ``SafeCmd.run()``: spawning the subprocess, wiring its
stream consumers, and assembling the ``CommandResult``. The rules for ending a
run — applying the deadline, terminating the process, and draining the stream
consumers exactly once — live in ``cuprum._subprocess_wait``. The streamed
run loop that waits for exit and reconciles the consumer tasks lives in
``cuprum._subprocess_stream_run``, the consumer construction those two drive
lives in ``cuprum._subprocess_streams``, and the spawn seam itself lives in
``cuprum._subprocess_spawn``. All three are re-exported here so importers of
this module keep working unchanged. Timing and child resource usage are
measured here, with ``cuprum._wait4_spawn`` owning the direct child's
``wait4`` reap and the aggregate fallback; ``_DirectRunStart`` keeps the three
pre-spawn readings together so their shared ordering cannot drift.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import _wait4_spawn
from cuprum._idle_heartbeat import _stop_idle_monitor
from cuprum._process_lifecycle import _shielded_cleanup
from cuprum._result_assembly import (
    _require_bytes,
    _require_captured_text,
    _require_text,
    _RunMeasurements,
)
from cuprum._subprocess_context import _sh_module
from cuprum._subprocess_spawn import _start_owned_process, _SubprocessExecution
from cuprum._subprocess_stream_run import (
    # Imported, not merely re-exported: the direct run path below is the caller.
    _run_subprocess_with_streams,
    _run_subprocess_without_streams,
)
from cuprum._subprocess_streams import (
    _build_stream_config as _build_stream_config,
)
from cuprum._subprocess_streams import (
    _create_stream_callback as _create_stream_callback,
)
from cuprum._subprocess_streams import (
    _spawn_stream_consumers as _spawn_stream_consumers,
)
from cuprum._subprocess_streams import (
    _StreamConsumerSpawnContext as _StreamConsumerSpawnContext,
)
from cuprum._subprocess_timeout import (
    _emit_exit_event,
    _ExitEventDetails,
    _handle_subprocess_timeout,
    _SubprocessTimeoutContext,
    _SubprocessTimeoutError,
)
from cuprum._subprocess_wait import (
    _wait_for_exit_code_within_timeout as _wait_for_exit_code_within_timeout,
)

if typ.TYPE_CHECKING:
    import asyncio

    from cuprum._pipeline_types import _StageObservation
    from cuprum._result_types import _AnyCommandResult
    from cuprum._rusage import _ChildRusageSnapshot
    from cuprum._streams import _RelayDiagnostics
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.echo_events import RelayFallback


@dc.dataclass(frozen=True, slots=True)
class _DirectRunStart:
    """The three readings every direct run takes before its child exists.

    They are captured as one record because they share a single ordering
    invariant rather than merely happening to sit together: all three are read
    *before* the spawn await, so a run's recorded duration includes the time
    its spawn blocked, and the rusage bracket spans the child's whole life.
    Sampling any of them after the await would silently change what the
    published figures mean, so the type keeps the readings from being taken
    apart and reordered.
    """

    rusage_before: _ChildRusageSnapshot | None
    monotonic_started_at: float
    wall_clock_started_at: float


def _sample_run_start(observation: _StageObservation) -> _DirectRunStart:
    """Take the pre-spawn readings, in the order the timing tests pin."""
    return _DirectRunStart(
        rusage_before=_wait4_spawn.capture_resource_before_spawn(),
        # The deadline is a remainder from this clock, so both come from it.
        monotonic_started_at=observation.monotonic_clock(),
        wall_clock_started_at=observation.wall_clock(),
    )


@dc.dataclass(frozen=True, slots=True)
class _DirectCompletion:
    """The settled facts of a direct run, once its streams have reconciled.

    ``stdout_text``, ``stderr_text``, and ``relay_diagnostics`` stay ``None``
    on the direct path, which captures nothing; the stream path overwrites all
    three. Returning them as one record keeps the waiting and the result
    assembly in separate helpers, so neither accumulates enough locals to trip
    the repository's ``too-many-locals`` ceiling.

    The two payload fields are typed ``str | bytes`` because the drain reports
    whichever the run's mode asked for. Nothing here inspects the type: the
    mode is read once, at the seam that chooses which result class to build,
    so the payload reaches its field unexamined.
    """

    exit_code: int
    exited_at: float
    stdout_text: _StreamPayload | None
    stderr_text: _StreamPayload | None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None


def _relay_fallbacks_for_result(
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None,
) -> tuple[RelayFallback, ...]:
    """Flatten per-stream diagnostics into one result tuple.

    The order is stdout's records then stderr's; each record carries its own
    stream, and this order does not reconstruct chronological interleaving
    between the two streams.

    Returns
    -------
    tuple[RelayFallback, ...]
        The command's categorical echo-failure and delivery-loss records,
        empty when its diagnostics are absent or recorded nothing.
    """
    if relay_diagnostics is None:
        return ()
    return relay_diagnostics[0].snapshot() + relay_diagnostics[1].snapshot()


async def _await_direct_completion(
    process: asyncio.subprocess.Process,
    execution: _SubprocessExecution,
    *,
    pid: int | None,
    started_at: float,
) -> _DirectCompletion:
    """Wait for the child to settle and report how it ended as one record."""
    # The direct path captures nothing; the stream path overwrites these values.
    stdout_text: _StreamPayload | None = None
    stderr_text: _StreamPayload | None = None
    relay_diagnostics: tuple[_RelayDiagnostics, _RelayDiagnostics] | None = None
    try:
        if execution.consumes_stdout or execution.consumes_stderr:
            (
                exit_code,
                exited_at,
                stdout_text,
                stderr_text,
                relay_diagnostics,
            ) = await _run_subprocess_with_streams(
                process,
                execution,
                pid=pid,
            )
        else:
            exit_code, exited_at = await _run_subprocess_without_streams(
                process,
                execution,
            )
    except (TimeoutError, _SubprocessTimeoutError) as exc:
        _handle_subprocess_timeout(
            _SubprocessTimeoutContext(
                execution=execution,
                process=process,
                started_at=started_at,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
            ),
            exc,
        )
    finally:
        # Every exit path settles the watchdog exactly once, including the
        # failures converted above and any that bypass the stream helpers
        # entirely; repeats are no-ops.
        await _shielded_cleanup(_stop_idle_monitor(execution.idle))

    return _DirectCompletion(
        exit_code=exit_code,
        exited_at=exited_at,
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        relay_diagnostics=relay_diagnostics,
    )


async def _execute_subprocess(execution: _SubprocessExecution) -> _AnyCommandResult:
    """Execute a subprocess and return the command result."""
    # All three pre-spawn readings are taken here, before the spawn await, so a
    # run's recorded duration includes the time its spawn blocked and the rusage
    # bracket spans the child's whole life. ``started_at`` is the monotonic
    # reference the duration and every line stamp are measured against;
    # ``wall_clock_started_at`` is the separate wall-clock instant the result
    # reports to callers.
    run_start = _sample_run_start(execution.observation)
    started_at = run_start.monotonic_started_at
    started = await _start_owned_process(execution, started_at)
    process = started.process
    execution = started.execution
    pid = process.pid
    completion = await _await_direct_completion(
        process,
        execution,
        pid=pid,
        started_at=started_at,
    )

    rusage = _wait4_spawn.resource_usage_for(process, run_start.rusage_before)
    _emit_exit_event(
        execution.observation,
        _ExitEventDetails(
            pid=pid,
            exit_code=completion.exit_code,
            started_at=started_at,
            exited_at=completion.exited_at,
            # The same measurement the returned result carries, so a consumer
            # reading the event stream sees the figures the caller sees rather
            # than having to correlate an event with a result object.
            resource_usage=rusage,
        ),
    )
    return _build_command_result(
        execution,
        completion,
        _RunMeasurements(
            pid=pid if pid is not None else -1,
            started_at=run_start.wall_clock_started_at,
            duration=max(0.0, completion.exited_at - started_at),
            rusage=rusage,
            resolved_path=execution.observation.resolved_path,
            relay_fallbacks=_relay_fallbacks_for_result(completion.relay_diagnostics),
        ),
    )


def _build_command_result(
    execution: _SubprocessExecution,
    completion: _DirectCompletion,
    measurements: _RunMeasurements,
) -> _AnyCommandResult:
    """Assemble the run's result, choosing the class its mode calls for.

    This is the only place either result class is constructed, and the only
    place the run's mode is read on the way out. One builder rather than two
    keeps every measurement — duration, rusage, the relay diagnostics — from
    being computed twice under two names, which is how a binary run would come
    to report figures that a text run of the same command did not. The two
    constructions differ only in the class and in the narrowing of the captured
    payloads, and both narrowing rules are read from
    :mod:`cuprum._result_assembly` rather than restated here.

    Returns
    -------
    CommandResult | BytesCommandResult
        A ``BytesCommandResult`` when the run captured bytes, a
        ``CapturedResult`` when it captured text, or an ordinary
        ``CommandResult`` when capture was disabled.
    """
    sh = _sh_module()
    if execution.capture_bytes:
        return sh.BytesCommandResult(
            program=execution.cmd.program,
            argv=execution.cmd.argv,
            exit_code=completion.exit_code,
            stdout=_require_bytes(completion.stdout_text, "stdout"),
            stderr=_require_bytes(completion.stderr_text, "stderr"),
            **measurements.as_kwargs(),
        )
    if execution.capture:
        return sh.CapturedResult(
            program=execution.cmd.program,
            argv=execution.cmd.argv,
            exit_code=completion.exit_code,
            stdout=_require_captured_text(
                completion.stdout_text,
                "stdout",
                program=execution.cmd.program,
                exec_id=str(execution.observation.exec_id),
            ),
            stderr=_require_captured_text(
                completion.stderr_text,
                "stderr",
                program=execution.cmd.program,
                exec_id=str(execution.observation.exec_id),
            ),
            **measurements.as_kwargs(),
        )
    return sh.CommandResult(
        program=execution.cmd.program,
        argv=execution.cmd.argv,
        exit_code=completion.exit_code,
        stdout=_require_text(completion.stdout_text, "stdout"),
        stderr=_require_text(completion.stderr_text, "stderr"),
        **measurements.as_kwargs(),
    )


# The execution bundle, its spawn start, the spawn seam itself, and the
# stream-consumer trio are re-exported from ``cuprum._subprocess_spawn`` and
# ``cuprum._subprocess_streams``. They stay in this module's namespace because
# the single-command run, the line-stream coordinator, the owned-handle start,
# and the test modules that replace them all reach for the orchestration
# module's own name.
__all__ = [
    "_DirectCompletion",
    "_DirectRunStart",
    "_StreamConsumerSpawnContext",
    "_SubprocessExecution",
    "_build_stream_config",
    "_create_stream_callback",
    "_execute_subprocess",
    "_run_subprocess_with_streams",
    # Re-exported so the tests that neutralize the direct path can keep
    # patching it where they always have.
    "_run_subprocess_without_streams",
    "_spawn_stream_consumers",
    # Re-exported so existing imports resolve: the deadline wait moved to
    # ``cuprum._subprocess_wait``. It supports imports only, so a test must
    # patch ``_subprocess_stream_run`` or ``_line_stream.coordinator``.
    "_wait_for_exit_code_within_timeout",
]
