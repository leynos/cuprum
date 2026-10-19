"""Re-authorize a prepared line stream in the context that consumes it.

``SafeCmd.lines()`` returns a lazy stream: it resolves the allowlist, the
timeout, and every instrument it needs, and only starts the subprocess when the
first event is requested. Those two moments can fall in different scopes, and a
prepared stream is not permission to run — so the checks that matter are
repeated here, in the task that pulls the first event.

This module owns that repeat. It also owns the cleanup a *failed* repeat needs:
the execution bundle may already have started the bounded output-delivery
worker, and a denied run must not leave that daemon thread parked.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

# Runtime imports, not ``TYPE_CHECKING`` ones: both are called here. Neither
# module imports ``cuprum._line_stream``, ``cuprum.sh``, or
# ``cuprum._line_iteration``, so this cannot close a cycle.
from cuprum._context_policy import _enforce_allowlist
from cuprum._subprocess_context import _resolve_timeout

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution

__all__ = ["_refresh_line_stream_execution"]


def _refresh_line_stream_execution(
    execution: _SubprocessExecution,
    requested_timeout: float | None,
) -> _SubprocessExecution:
    """Re-check the allowlist and re-resolve the deadline for a pending pull.

    Called from the drive generator's body, which runs in the context of the
    task awaiting the stream. The allowlist check therefore applies the scope
    in force *now*, not the one that existed when ``lines()`` was called; a
    program that has since been forbidden raises
    :class:`~cuprum.context.ForbiddenProgramError` here, before a queue, a
    task, a plan event, or a hook exists.

    The deadline is resolved with the same precedence ``run()`` applies, but
    the ambient term is read here rather than at construction: an explicit
    *requested_timeout* (including ``0``) wins, then the per-call
    ``ExecutionContext.timeout``, then the consuming scope's timeout. Passing
    ``None`` deliberately falls through to those fallbacks rather than
    disabling timeouts.

    Returns
    -------
    _SubprocessExecution
        A copy of *execution* carrying the freshly resolved timeout. The
        input is unchanged, matching every other refresh of this frozen
        bundle.

    Raises
    ------
    ForbiddenProgramError
        If the active context's allowlist forbids ``execution.cmd.program``.
    """  # ruff: ignore[docstring-extraneous-exception] - the refusal propagates from the allowlist check
    try:
        _enforce_allowlist(execution.cmd)
        effective_timeout = _resolve_timeout(
            timeout=requested_timeout,
            context=execution.ctx,
        )
    except BaseException as refusal:
        # The bundle may already own a delivery thread: preparing the
        # execution builds one, and nothing has been handed to a coordinator
        # that could close it on this path.
        _close_refused_delivery_worker(execution, refusal)
        raise
    return dc.replace(execution, timeout=effective_timeout)


def _close_refused_delivery_worker(
    execution: _SubprocessExecution,
    refusal: BaseException,
) -> None:
    """Stop a refused run's delivery worker, noting any close failure on it."""
    worker = execution.delivery_worker
    if worker is None:
        return
    try:
        worker.close(grace=0.0)
    except BaseException as close_error:  # ruff: ignore[blind-except] - preserve the refusal.
        refusal.add_note(
            f"Output delivery worker cleanup also failed "
            f"({type(close_error).__name__})."
        )
