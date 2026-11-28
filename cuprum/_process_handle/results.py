"""The result half of an owned handle's settlement.

``coordinator`` owns *when* a run settles: the wait, the reconciliation, and
the one delivery-worker close. This module owns *what* it reports: the settled
record, the text and byte result classes, and the narrowing that keeps each
mode's payload type honest. Keeping the two apart is what let the coordinator
accept the live-byte hooks and the shared result assembler without crossing the
repository's 400-line ceiling.

``cuprum._bytes_run`` imports the ``cuprum.sh`` result modules, so the
narrowing helpers it provides are imported inside the assembler rather than at
module scope: this module is reached while that package is still initializing.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum import _wait4_spawn
from cuprum._result_assembly import (
    _recorded_captures,
    _require_bytes,
    _require_text,
    _RunMeasurements,
)
from cuprum._subprocess_context import _sh_module
from cuprum._subprocess_outcome import _relay_fallbacks_for_result
from cuprum._subprocess_timeout import _emit_run_exit_event

if typ.TYPE_CHECKING:
    from cuprum._process_handle.tasks import _HandleRun
    from cuprum._subprocess_outcome import _SharedResultFields
    from cuprum._subprocess_wait_types import _DrainPair, _StreamPayload
    from cuprum.capture_policy import CapturedStream
    from cuprum.sh import BytesCommandResult, CommandResult

__all__ = [
    "_HandleOutcome",
    "_assemble_handle_result",
    "_build_handle_result",
    "_handle_outcome",
]


def _handle_outcome(
    exit_code: int,
    exited_at: float,
    drained: _DrainPair,
) -> _HandleOutcome:
    """Build the settled record both handle modes publish from one drain.

    Returns
    -------
    _HandleOutcome
        The exit code, exit clock, drained payloads, and capture records.
    """
    return _HandleOutcome(
        exit_code=exit_code,
        exited_at=exited_at,
        stdout_payload=drained[0].payload,
        stderr_payload=drained[1].payload,
        captures=(drained[0].capture, drained[1].capture),
    )


@dc.dataclass(frozen=True, slots=True)
class _HandleOutcome:
    """The settled facts of an owned run, ready to be published.

    The exit code and the drained payloads are produced by whichever route
    reached the end of the wait, so they travel as one record rather than as
    four arguments apiece. That keeps the publishing helper's signature fixed
    no matter which route reached it, and keeps every route's result carrying
    the same fields.

    The payloads stay wide — the drain serves both modes — and each result
    builder narrows them with ``_require_text`` or ``_require_bytes`` before it
    constructs its class.
    """

    exit_code: int
    exited_at: float
    stdout_payload: _StreamPayload | None
    stderr_payload: _StreamPayload | None
    # The capture records for the two streams, in the stdout-then-stderr order
    # every pair in this library uses. Carried here rather than read from the
    # drain again, because the drain happens exactly once per run and its
    # records are the only description of how each policy retained its stream.
    captures: tuple[CapturedStream | None, CapturedStream | None] = (None, None)


def _build_handle_result(
    run: _HandleRun,
    outcome: _HandleOutcome,
    *,
    capture_bytes: bool,
) -> CommandResult | BytesCommandResult:
    """Publish the run's exit event and assemble its result.

    The event and the result are built together so a consumer observes the
    same exit code the caller's result carries. The result's ``duration`` is
    measured from the spawn, so it spans the time the handle was held — the
    child's lifetime, not the caller's wait.

    The shared fields are assembled once and each mode adds only its own
    narrowing of the two payloads. A text handle narrows to ``str``; a byte
    handle narrows to ``bytes``. Neither computes a measurement twice.

    Returns
    -------
    CommandResult | BytesCommandResult
        A ``BytesCommandResult`` when *capture_bytes* is true, otherwise the
        text ``CommandResult``. Both carry the captured streams, the exit code,
        the resource measurements the child's own reap produced, and the
        relay-diagnostic fields the run recorded.
    """
    execution = run.execution
    # The ``None`` before-snapshot is deliberate rather than an omission: it
    # selects the child's own ``wait4`` figure, which the kernel attributes to
    # this PID whenever the wait began, and it makes the aggregate
    # ``RUSAGE_CHILDREN`` delta unreachable — that variant needs a before-spawn
    # snapshot this path has no honest way to take, so it yields ``None``
    # instead of a figure measuring a window that would not be the child's.
    # Reporting nothing at all was the wrong default for the backend that can
    # answer: it left ``max_rss_bytes`` unavailable on a handle result while
    # ``run()`` reported it for the same command.
    rusage = _wait4_spawn.resource_usage_for(run.process, None)
    _emit_run_exit_event(
        execution,
        outcome,
        pid=run.process.pid,
        resource_usage=rusage,
    )
    # The same measurement record the direct run builds, so the handle cannot
    # report a figure the same command's ``run()`` reports differently. The
    # exit event above carries those figures too, which is what lets a consumer
    # of the event stream agree with the caller's result without correlating
    # two records.
    measurements = _RunMeasurements(
        pid=run.process.pid if run.process.pid is not None else -1,
        started_at=run.wall_clock_started_at,
        duration=max(0.0, outcome.exited_at - run.started_at),
        rusage=rusage,
        # Carried through from the observation for the same reason ``run()``
        # carries it: the handle spawns the bound executable, so a result that
        # reported ``None`` here would claim no binding applied while the
        # caller's binding is exactly what chose the file that ran.
        resolved_path=execution.observation.resolved_path,
        relay_fallbacks=_relay_fallbacks_for_result(run.ownership.relay_diagnostics),
    )
    shared: _SharedResultFields = {
        "program": execution.cmd.program,
        "argv": execution.cmd.argv,
        "exit_code": outcome.exit_code,
        **measurements.as_kwargs(),
        # Flattened through the same helper every other result-construction
        # site uses, so a handle that captured one stream reports exactly the
        # one record a ``run()`` of the same command would: the raw optional
        # pair would put a ``None`` in a tuple the field declares as holding
        # only records.
        "captures": _recorded_captures(outcome.captures),
    }
    sh = _sh_module()
    if capture_bytes:
        return sh.BytesCommandResult(
            **shared,
            stdout=_require_bytes(outcome.stdout_payload, "stdout"),
            stderr=_require_bytes(outcome.stderr_payload, "stderr"),
        )
    return sh.CommandResult(
        **shared,
        stdout=_require_text(outcome.stdout_payload, "stdout"),
        stderr=_require_text(outcome.stderr_payload, "stderr"),
    )


@typ.overload
def _assemble_handle_result(
    run: _HandleRun,
    outcome: _HandleOutcome,
    *,
    capture_bytes: typ.Literal[False],
) -> CommandResult: ...


@typ.overload
def _assemble_handle_result(
    run: _HandleRun,
    outcome: _HandleOutcome,
    *,
    capture_bytes: typ.Literal[True],
) -> BytesCommandResult: ...


def _assemble_handle_result(
    run: _HandleRun,
    outcome: _HandleOutcome,
    *,
    capture_bytes: bool,
) -> CommandResult | BytesCommandResult:
    """Build and narrow a settled handle result for its output mode.

    The narrowing helpers live in ``cuprum._bytes_run``, which imports the
    ``cuprum.sh`` result modules. They are imported here, not at module scope,
    because this module is reached while that package is still initializing.
    One assembler keeps the text and byte modes from drifting on which result
    class their mode requires.

    Returns
    -------
    CommandResult | BytesCommandResult
        The narrowed result: ``BytesCommandResult`` in byte mode, otherwise
        the text ``CommandResult``.
    """
    from cuprum._bytes_run import (
        _require_bytes_command_result,
        _require_command_result,
    )

    if capture_bytes:
        return _require_bytes_command_result(
            _build_handle_result(run, outcome, capture_bytes=True)
        )
    return _require_command_result(
        _build_handle_result(run, outcome, capture_bytes=False)
    )
