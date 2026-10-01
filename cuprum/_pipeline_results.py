"""Per-stage terminal events and result assembly for pipelines.

Split from ``cuprum._pipeline_internals`` so that module stays about *running*
a pipeline — spawning, waiting, cleanup — while the rules for reporting each
stage live here: the actual child ``exit`` event, definitive ``settled`` event,
and ``CommandResult`` assembled alongside them.

Both the success path and the timeout path emit that terminal event from this
module, so a stage never reports a ``timeout`` and then falls silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import time
import typing as typ

from cuprum._pipeline_collect import _sh_module
from cuprum._pipeline_types import _EventDetails, _ExecutionInvariantError
from cuprum._result_assembly import _require_bytes, _require_text
from cuprum._result_types import _AnyCommandResult, _AnyPipelineResult
from cuprum._sink_lifecycle import _outcome_for_result
from cuprum._timeout_reporting import _safe_emit_terminal
from cuprum.events import ResourceUsageMode, TerminalOutcome

# Runtime, not ``TYPE_CHECKING``: the narrowing helpers below use it with
# ``isinstance``.
from cuprum.sh.results import BytesCommandResult

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import (
        _PipelineSpawnResult,
        _PipelineStageResultInputs,
        _StageObservation,
    )
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.echo_events import RelayFallback
    from cuprum.sh import CommandResult, PipelineResult, SafeCmd

# Every pipeline stage's terminal event reports this. Both exits below are
# terminal events that attempted no measurement, and a stage can never attempt
# one: its children are reaped concurrently, so no per-child interface can
# attribute usage to it. Saying so explicitly is what keeps the contract on
# ``ExecEvent.resource_usage_mode`` — set on every terminal event, ``None`` on
# every other phase — true for stages as well as for direct commands, whose
# returned ``CommandResult`` likewise leaves all three figures ``None``.
_STAGE_RESOURCE_MODE: typ.Final = ResourceUsageMode.UNAVAILABLE


def _emit_terminal_events(
    observations: tuple[_StageObservation, ...],
    outcome: TerminalOutcome,
    *,
    processes: cabc.Sequence[asyncio.subprocess.Process] = (),
    started_at: cabc.Sequence[float] = (),
) -> None:
    """Settle every planned stage with the facts known after cleanup."""
    ended_at = time.perf_counter()
    for idx, observation in enumerate(observations):
        process = processes[idx] if idx < len(processes) else None
        started = started_at[idx] if idx < len(started_at) else None
        _safe_emit_terminal(
            observation,
            outcome,
            _EventDetails(
                pid=None if process is None else process.pid,
                exit_code=None if process is None else process.returncode,
                duration_s=(None if started is None else max(0.0, ended_at - started)),
            ),
        )


def _emit_result_terminal_events(
    observations: tuple[_StageObservation, ...],
    stage_results: list[CommandResult],
    *,
    outcome: TerminalOutcome | None = None,
    best_effort: bool = False,
) -> None:
    """Settle every assembled stage, surfacing the first hook failure by default."""
    first_error: BaseException | None = None
    for observation, result in zip(observations, stage_results, strict=True):
        stage_outcome = (
            _outcome_for_result(result).outcome if outcome is None else outcome
        )
        try:
            observation.emit_terminal(
                stage_outcome,
                _EventDetails(
                    pid=observation.started_pid,
                    exit_code=result.exit_code,
                    duration_s=result.duration,
                ),
            )
        # Try all stage observers even for fatal hook failures, then preserve
        # the first one so cleanup of the other stages cannot mask it.
        except BaseException as exc:  # ruff: ignore[blind-except]
            if first_error is None:
                first_error = exc
    if first_error is not None and not best_effort:
        raise first_error


def _emit_timeout_exit_events(
    observations: tuple[_StageObservation, ...],
    spawn: _PipelineSpawnResult,
) -> None:
    """Emit the terminal ``exit`` event for every stage reaped by a timeout.

    The success path emits these from :func:`_build_pipeline_stage_results`,
    which a timeout never reaches — it raises out of `_collect_pipeline_inputs`
    first. The single-command path has no such gap, since
    `_handle_subprocess_timeout` emits ``exit`` before raising
    ``TimeoutExpired``, so without this a pipeline stage would be the only
    execution that reports a ``timeout`` and then goes quiet.

    That matters beyond symmetry: ``TracingHook`` keeps a span open through
    ``exit`` and closes it only on ``settled``, so a missing terminal event
    leaves the stage's span open for the lifetime of the tracer.

    Every stage has been terminated and reaped by this point, so ``returncode``
    is available; ``-1`` stands in for a stage with no recorded code, matching
    ``_get_exit_code``.

    Each stage emits best-effort, matching ``_timeout_reporting._safe_emit``.
    :meth:`_StageObservation.emit` re-raises a synchronous observe-hook
    failure, and this runs inside the caller's ``except TimeoutExpired``
    handler: letting one escape would replace the ``TimeoutExpired`` the caller
    is about to re-raise *and* abandon the remaining stages, leaving exactly
    the open spans this function exists to close. ``emit`` records any
    scheduled async-hook tasks before raising, so those are still drained by
    the runner; only the synchronous failure is swallowed.
    """
    ended_at = time.perf_counter()
    for idx, obs in enumerate(observations):
        process = spawn.processes[idx]
        with contextlib.suppress(Exception, asyncio.CancelledError):
            obs.emit(
                "exit",
                _EventDetails(
                    pid=process.pid,
                    exit_code=(
                        process.returncode if process.returncode is not None else -1
                    ),
                    duration_s=max(0.0, ended_at - spawn.stages.started_at[idx]),
                    resource_usage_mode=_STAGE_RESOURCE_MODE,
                ),
            )


def _build_pipeline_stage_results(
    observations: tuple[_StageObservation, ...],
    *,
    processes: list[asyncio.subprocess.Process],
    inputs: _PipelineStageResultInputs,
    capture_bytes: bool = False,
) -> list[_AnyCommandResult]:
    """Emit exit events and assemble a command result per pipeline stage.

    The observation a stage already has supplies its command and its index, so
    the stage list is not passed again: ``len(observations)`` is the stage
    count the final-stage check reads, and threading the commands separately
    would only create a second source that could disagree with it.

    ``capture_bytes`` selects the class of the results built here, exactly as
    it does on the single-command path: every stage of one pipeline reports its
    captured streams in the same mode, because the mode is a property of the
    caller's request rather than of a stage.

    Returns
    -------
    list[CommandResult | BytesCommandResult]
        One result per stage, in execution order, of whichever class the mode
        selects.
    """
    stage_results: list[_AnyCommandResult] = []
    for idx, obs in enumerate(observations):
        process = processes[idx]
        ended_at = inputs.wait_result.ended_at[idx]
        duration_s = (
            None
            if ended_at is None
            else max(0.0, ended_at - inputs.wait_result.started_at[idx])
        )
        obs.emit(
            "exit",
            _EventDetails(
                pid=process.pid,
                exit_code=inputs.wait_result.exit_codes[idx],
                duration_s=duration_s,
                resource_usage_mode=_STAGE_RESOURCE_MODE,
            ),
        )
        stage_results.append(
            _build_stage_result(
                _StageResultInputs(
                    cmd=obs.cmd,
                    exit_code=inputs.wait_result.exit_codes[idx],
                    pid=process.pid if process.pid is not None else -1,
                    started_at=inputs.wait_result.wall_clock_started_at[idx],
                    duration=0.0 if duration_s is None else duration_s,
                    relay_fallbacks=inputs.relay_fallbacks_by_stage[idx],
                ),
                inputs.final_stdout if idx == len(observations) - 1 else None,
                inputs.stderr_by_stage[idx],
                capture_bytes=capture_bytes,
            ),
        )
    return stage_results


@dc.dataclass(frozen=True, slots=True)
class _StageResultInputs:
    """The per-stage facts a stage result is assembled from.

    A stage is never measured — its children are reaped concurrently, so no
    per-child interface can attribute usage to it — which leaves the five
    positional fields below and the two captured streams, one argument too many
    for the repository's call-surface ceiling. Grouping them here is also what
    keeps the two constructions *identical* apart from their streams: the
    resource figures are written as ``None`` in one place rather than in each
    branch.
    """

    cmd: SafeCmd
    exit_code: int
    pid: int
    started_at: float
    duration: float
    relay_fallbacks: tuple[RelayFallback, ...]


def _build_stage_result(
    inputs: _StageResultInputs,
    stdout: _StreamPayload | None,
    stderr: _StreamPayload | None,
    *,
    capture_bytes: bool,
) -> _AnyCommandResult:
    """Build one stage's result, in the mode the pipeline was asked for.

    The mode is the same for every stage, so the two constructions here are the
    pipeline's counterpart to the single-command result seam, and both live
    behind one helper for the same reason: a stage result and a direct result
    of the same command should differ only in what could actually be measured.

    Returns
    -------
    CommandResult | BytesCommandResult
        A ``BytesCommandResult`` when the pipeline captured bytes, otherwise
        the ordinary text result.
    """
    sh = _sh_module()
    measurements = {
        "program": inputs.cmd.program,
        "argv": inputs.cmd.argv,
        "exit_code": inputs.exit_code,
        "pid": inputs.pid,
        "started_at": inputs.started_at,
        "duration": inputs.duration,
        # A stage can never be measured, so all three figures stay ``None``.
        "max_rss_bytes": None,
        "user_cpu_seconds": None,
        "system_cpu_seconds": None,
        "relay_fallbacks": inputs.relay_fallbacks,
    }
    if capture_bytes:
        return sh.BytesCommandResult(
            stdout=_require_bytes(stdout, "stdout"),
            stderr=_require_bytes(stderr, "stderr"),
            **measurements,
        )
    return sh.CommandResult(
        stdout=_require_text(stdout, "stdout"),
        stderr=_require_text(stderr, "stderr"),
        **measurements,
    )


def _require_bytes_stage(stage: _AnyCommandResult) -> BytesCommandResult:
    """Narrow a stage result the pipeline's mode says is byte-exact.

    The stage builder already chose the class from the same flag, so this is
    the aggregate's half of the narrowing :mod:`cuprum._result_assembly`
    performs per payload: a text stage inside a ``BytesPipelineResult`` would
    be a type that lies about what it carries.

    Returns
    -------
    BytesCommandResult
        The same stage, narrowed to the byte-exact class.

    Raises
    ------
    _ExecutionInvariantError
        If a byte-exact pipeline built a text stage.
    """
    if not isinstance(stage, BytesCommandResult):
        msg = "byte-exact pipeline produced a text stage result"
        raise _ExecutionInvariantError(msg)
    return stage


def _require_text_stage(stage: _AnyCommandResult) -> CommandResult:
    """Narrow a stage result the pipeline's mode says is text.

    ``BytesCommandResult`` does not subclass ``CommandResult``, so a stage of
    the wrong class is a contradiction rather than a subtype to accept.

    Returns
    -------
    CommandResult
        The same stage, narrowed to the text class.

    Raises
    ------
    _ExecutionInvariantError
        If a text pipeline built a byte-exact stage.
    """
    if isinstance(stage, BytesCommandResult):
        msg = "text pipeline produced a byte-exact stage result"
        raise _ExecutionInvariantError(msg)
    return stage


def _build_pipeline_result(
    stage_results: list[_AnyCommandResult],
    *,
    failure_index: int | None,
    capture_bytes: bool,
) -> _AnyPipelineResult:
    """Wrap the stage results in the pipeline result their mode calls for.

    The stages were already built in the run's mode, so the aggregate has to
    follow them: a ``BytesPipelineResult`` whose ``stages`` held text results
    would be a type that lies about what it carries, and its ``stdout``
    property reads the final stage.

    Returns
    -------
    PipelineResult | BytesPipelineResult
        A ``BytesPipelineResult`` when the pipeline captured bytes, otherwise
        the ordinary text result.
    """
    sh = _sh_module()
    if capture_bytes:
        return sh.BytesPipelineResult(
            stages=tuple(_require_bytes_stage(stage) for stage in stage_results),
            failure_index=failure_index,
        )
    return sh.PipelineResult(
        stages=tuple(_require_text_stage(stage) for stage in stage_results),
        failure_index=failure_index,
    )


__all__ = [
    "_build_pipeline_result",
    "_build_pipeline_stage_results",
    "_emit_result_terminal_events",
    "_emit_terminal_events",
    "_emit_timeout_exit_events",
    "_require_bytes_stage",
    "_require_text_stage",
]
