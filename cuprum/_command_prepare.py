"""The preparation every command shares before a child exists.

``SafeCmd.run()`` and ``SafeCmd.start()`` open the same presentation-sink
session, prepare the same observation, emit the same ``plan`` event, run the
same before hooks, and build the same execution bundle. Keeping that prefix in
one place is what stops the two entry points drifting apart: a change to the
plan event's tags, or to the order the bracket opens in, lands for both.

The prefix owns its own failure handling. A plan observer or a before hook can
raise before any child exists, and the session it opened still has to be closed
and the tasks it scheduled drained — so the finalization runs here, where the
tracking and observation that describe it are still in hand.

The three helpers this module borrows from ``cuprum._command_internals`` are
imported inside the function, not at module scope: that module imports
``_prepare_owned_command`` for the ``run()`` path, so a module-level import
would close the cycle.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

from cuprum._execution_tracking import _ExecutionTracking
from cuprum._pipeline_internals import _collect_hooks
from cuprum._pipeline_types import _EventDetails
from cuprum._sink_lifecycle import _command_session_start, _SinkBracket
from cuprum.context import current_context

if typ.TYPE_CHECKING:
    from cuprum._command_internals import _ExecutionState
    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import SafeCmd

__all__ = ["_PreparedCommand", "_prepare_owned_command"]


@dc.dataclass(frozen=True, slots=True)
class _PreparedCommand:
    """A validated command's prepared execution, ready to spawn.

    The bundle and its tracking travel together because neither is usable
    without the other: the tracking owns the sink bracket the bundle's session
    was opened from and the pending-task list the run's finalization drains, so
    a caller holding one alone would close a session it never opened, or drain
    tasks it never collected.
    """

    execution: _SubprocessExecution
    tracking: _ExecutionTracking


async def _prepare_owned_command(
    cmd: SafeCmd,
    state: _ExecutionState,
) -> _PreparedCommand:
    """Open the run's session, prepare its observation, and run its before hooks."""
    # Resolved here rather than at module scope so a test that replaces any of
    # the three on ``cuprum._command_internals`` still reaches the call site.
    from cuprum._command_internals import (
        _build_subprocess_execution,
        _finalize_prepared_command_failure,
        _prepare_execution_observation,
    )

    output = state.output
    # The bracket owns the session for the whole run: a plan observer, a before
    # hook, or anything else that raises before execution starts still
    # finalizes the adapter's framing rather than stranding an open group, and
    # the guard below closes exactly what those paths leave.
    delivery = output.delivery
    sink_bracket = _SinkBracket.open(
        output.sink,
        _command_session_start(cmd),
        bounded_delivery=delivery is not None,
        delivery_grace_s=None if delivery is None else delivery.drain_grace_s,
    )
    tracking = _ExecutionTracking(
        execution_hooks=_collect_hooks(current_context()),
        pending_tasks=[],
        sink_bracket=sink_bracket,
    )
    observation: _StageObservation | None = None
    try:
        observation = _prepare_execution_observation(
            cmd,
            state,
            tracking,
        )
        observation.emit("plan", _EventDetails(pid=None))
        for hook in tracking.execution_hooks.before_hooks:
            hook(cmd)
        # Built inside the guard, not after it: the run's sink framing is
        # already open at this point, so a bundle that cannot be constructed —
        # an unresolvable mirror sink, say — has to close that framing rather
        # than leave the session open behind the failure.
        execution = _build_subprocess_execution(
            cmd,
            state,
            observation=observation,
            sink_bracket=sink_bracket,
        )
    except BaseException as run_error:
        await _finalize_prepared_command_failure(
            tracking,
            observation,
            run_error,
        )
        raise
    return _PreparedCommand(execution=execution, tracking=tracking)
