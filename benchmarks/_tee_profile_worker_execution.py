"""Execution and result assembly for the tee hot-path profiling worker.

This module owns running one configured Cuprum command, driving the repeat
loop under an activated backend, and assembling the JSON-compatible
``TeeProfileWorkerResult`` payload returned by ``run_tee_profile_worker``.

Command construction lives in ``benchmarks._tee_profile_worker_command`` and
backend selection lives in ``benchmarks._tee_profile_worker_backend``; both
are imported here rather than re-implemented.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from benchmarks._tee_profile_worker_backend import (
    Clock,
    _default_clock,
    _EnvBackendSelector,
)
from benchmarks._tee_profile_worker_command import (
    _build_command,
    _capture_and_echo_flags,
    _manifest_hash,
    _result_exit_code,
)
from benchmarks.sinks import open_sink
from cuprum import ExecutionContext, ScopeConfig, scoped, sh
from cuprum._streams_pump import _current_read_size, _override_read_size

if typ.TYPE_CHECKING:
    from benchmarks._tee_profile_worker_backend import BackendSelector, _SelectorMetrics
    from benchmarks._tee_profile_worker_command import (
        WorkerCommandResult,
        _WorkerCommand,
    )
    from benchmarks._tee_profile_worker_config import (
        TeeProfileWorkerConfig,
        TeeProfileWorkerResult,
    )
    from cuprum import ExecEvent

__all__ = ["run_tee_profile_worker"]


@dc.dataclass(frozen=True, slots=True)
class _RunTotals:
    """Accumulated totals from one worker repeat loop."""

    # Why not inline this into ``_build_worker_result``: this type, together
    # with ``_TimingContext``, keeps that helper's argument list at four.
    # Inlining either grouping would re-trigger the PLR0913/CodeScene finding
    # that this PR already resolved, so this indirection is structural.

    captured_output_length: int
    stdout_line_count: int
    exit_code: int
    status: typ.Literal["ok", "failed"]


@dc.dataclass(frozen=True, slots=True)
class _TimingContext:
    """Wall-clock callable and the timestamp at which the worker run started."""

    # ``timer`` and ``started`` are a single timing concept. The wrapper keeps
    # that coupling explicit and shares the same "do not inline" rationale as
    # ``_RunTotals``: suppressing complexity findings for these bounded helper
    # types is preferable to regressing an already-resolved argument-count
    # diagnostic or weakening testability.

    timer: Clock
    started: float


def _run_command_sync(
    config: TeeProfileWorkerConfig,
    worker_cmd: _WorkerCommand,
    *,
    capture: bool,
    echo: bool,
) -> tuple[WorkerCommandResult, int]:
    """Run one configured command synchronously and count stdout line events.

    The ``observe_line`` closure mutates ``line_count`` via ``nonlocal``
    without a lock. This is safe because ``sh.observe`` calls the callback
    synchronously in the same thread that calls ``run_sync``; no concurrent
    access to ``line_count`` is possible.

    Returns
    -------
    tuple[WorkerCommandResult, int]
        The command result and the number of stdout line events observed.
    """
    line_count = 0

    def observe_line(event: ExecEvent) -> None:
        """Count stdout line callback events emitted during command execution."""
        nonlocal line_count
        if event.phase == "stdout" and event.line is not None:
            line_count += 1

    with open_sink(
        config.sink_kind,
        encoding=config.encoding,
        errors=config.errors,
    ) as sink:
        context = ExecutionContext(
            stdout_sink=sink,
            encoding=config.encoding,
            errors=config.errors,
        )
        output = sh.RunOutputOptions(capture=capture, echo=echo)
        with scoped(ScopeConfig(allowlist=worker_cmd.allowlist)):
            # SafeCmd and Pipeline now share the ``output=RunOutputOptions``
            # calling convention, so only the observe-hook wrapping differs.
            if config.with_line_callbacks:
                with sh.observe(observe_line):
                    result = worker_cmd.cmd.run_sync(output=output, context=context)
            else:
                result = worker_cmd.cmd.run_sync(output=output, context=context)
    return result, line_count


def _run_once(config: TeeProfileWorkerConfig) -> tuple[int, int, int]:
    """Run one Cuprum command and report its captured output and exit code.

    Returns
    -------
    tuple[int, int, int]
        ``(captured_output_length, exit_code, stdout_line_count)``. The first
        element is the length of the captured stdout — not a status value —
        and is ``0`` when nothing was captured.
    """
    worker_cmd = _build_command(config)
    capture, echo = _capture_and_echo_flags(config.mode)
    result, line_count = _run_command_sync(
        config,
        worker_cmd,
        capture=capture,
        echo=echo,
    )

    captured = result.stdout
    captured_len = len(captured) if captured is not None else 0
    return captured_len, _result_exit_code(result), line_count


def _run_repeat_loop(
    config: TeeProfileWorkerConfig,
    selector: BackendSelector,
) -> _RunTotals:
    """Execute the configured repeat loop and return accumulated totals."""
    total_captured_len = 0
    total_line_count = 0
    exit_code = 0
    status: typ.Literal["ok", "failed"] = "ok"
    with selector(config.backend):
        for _ in range(config.repeat_count):
            captured_len, exit_code, line_count = _run_once(config)
            total_captured_len += captured_len
            total_line_count += line_count
            if exit_code != 0:
                status = "failed"
                break
    return _RunTotals(
        captured_output_length=total_captured_len,
        stdout_line_count=total_line_count,
        exit_code=exit_code,
        status=status,
    )


def _scenario_label(config: TeeProfileWorkerConfig) -> str:
    """Build a compact label for ad hoc worker runs."""
    cb = "cb" if config.with_line_callbacks else "nocb"
    return f"{config.mode}-{config.sink_kind}-{cb}-s{config.stages}-{config.backend}"


def _build_worker_result(
    config: TeeProfileWorkerConfig,
    *,
    timing: _TimingContext,
    totals: _RunTotals,
    metrics: _SelectorMetrics,
) -> TeeProfileWorkerResult:
    """Assemble a ``TeeProfileWorkerResult`` from accumulated run data."""
    # Capture the elapsed worker-run time before any result-assembly work so
    # that ``_manifest_hash`` I/O and ``_scenario_label`` do not inflate the
    # measured ``wall_time_seconds``.
    wall_time_seconds = timing.timer() - timing.started
    return {
        "scenario": _scenario_label(config),
        "fixture_path": str(config.fixture_path),
        "fixture_manifest_hash": _manifest_hash(config.fixture_path),
        "stages": config.stages,
        "mode": config.mode,
        "sink_kind": config.sink_kind,
        "with_line_callbacks": config.with_line_callbacks,
        "backend": config.backend,
        "repeat_count": config.repeat_count,
        "read_size": _current_read_size(),
        "wall_time_seconds": wall_time_seconds,
        "lock_wait_seconds": metrics.lock_wait_seconds,
        "reentrant_rejection_count": metrics.reentrant_rejection_count,
        "status": totals.status,
        "exit_code": totals.exit_code,
        "captured_output_length": totals.captured_output_length,
        "stdout_line_count": totals.stdout_line_count,
    }


def run_tee_profile_worker(
    config: TeeProfileWorkerConfig,
    *,
    backend_selector: BackendSelector | None = None,
    clock: Clock | None = None,
) -> TeeProfileWorkerResult:
    """Execute a configured tee profiling worker and return a JSON payload.

    Parameters
    ----------
    config:
        Worker execution settings including fixture path, stage count, mode,
        sink kind, backend, and repeat count.
    backend_selector:
        Optional override for backend activation. Defaults to
        ``_EnvBackendSelector()``, which mutates ``os.environ``. Pass a custom
        implementation in tests to avoid side-effects.
    clock:
        Optional wall-clock callable. Defaults to ``time.perf_counter``. Pass a
        deterministic stub in tests to avoid timing non-determinism.

    Returns
    -------
    TeeProfileWorkerResult
        Result mapping with keys ``scenario`` (str), ``fixture_path`` (str),
        ``fixture_manifest_hash`` (str or None), ``stages`` (int), ``mode``
        (str), ``sink_kind`` (str), ``with_line_callbacks`` (bool),
        ``backend`` (str), ``repeat_count`` (int), ``wall_time_seconds``
        (float), ``lock_wait_seconds`` (float),
        ``reentrant_rejection_count`` (int), ``status`` (``"ok"`` or
        ``"failed"``), ``exit_code`` (int),
        ``captured_output_length`` (int), and ``stdout_line_count`` (int).
    """
    timer = clock if clock is not None else _default_clock
    selector = (
        backend_selector
        if backend_selector is not None
        else _EnvBackendSelector(clock=timer)
    )
    metrics_state = selector.metrics_state
    metrics_state.reset()
    timing = _TimingContext(timer=timer, started=timer())
    with _override_read_size(config.read_size):
        totals = _run_repeat_loop(config, selector)
        metrics = metrics_state.snapshot()
        return _build_worker_result(
            config,
            timing=timing,
            totals=totals,
            metrics=metrics,
        )
