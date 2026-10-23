"""The pre-flight rules a line iteration applies to one command's options.

Split from ``cuprum.sh.safe_cmd`` when the rebase onto ``main`` pushed that
module past the 400-line ceiling. The helpers are about the same moment: the
point *before* a line iteration is built, where the options are checked and the
bookkeeping the iteration travels with is assembled. Neither touches the class,
so keeping them beside it only made the module longer.

They live in the ``cuprum.sh`` package rather than with ``_line_iteration``
because both are statements about this package's own option and hook types;
the driver that consumes them stays in ``cuprum._line_iteration``.

``cuprum.sh.safe_cmd`` re-exports these names. That keeps the module's globals
the ones a test monkeypatching or introspecting this path already reads, and
keeps the facade's import list unchanged.

This module does carry ``from __future__ import annotations``: unlike its
sibling ``safe_cmd``, nothing here is a public signature reached by
``typing.get_type_hints``, so there is no eagerly-evaluated surface to protect.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import typing as typ

from cuprum._bounded_encoding import _reject_unsupported_bounded_captures
from cuprum._command_internals import (
    _build_subprocess_execution,
    _ExecutionState,
    _prepare_execution_observation,
)
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._pipeline_internals import _collect_hooks, _enforce_allowlist
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._subprocess_context import _resolve_timeout
from cuprum.context import current_context
from cuprum.sh.execution import _resolve_stdin_source
from cuprum.sh.stdio_rules import _reject_contested_stdin

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh.execution import ExecutionContext, StdinSource, StdinStream
    from cuprum.sh.output import RunOutputOptions
    from cuprum.sh.safe_cmd import SafeCmd

__all__ = [
    "_line_iteration_tracking",
    "_prepare_line_iteration",
    "_reject_line_iteration_conflicts",
    "_reject_redirected_lines_stdout",
    "_resolve_line_stdin_source",
]


def _reject_line_iteration_conflicts(
    cmd: SafeCmd,
    output: RunOutputOptions,
    context: ExecutionContext,
) -> None:
    """Refuse everything that stops these options becoming a line iteration.

    All three refusals decide the same thing — whether this command can be
    iterated line by line at all — and all three must happen before the lazy
    execution is built. The bounded-capture rule in particular would otherwise
    fire only when the first event was pulled, after the caller had already
    started consuming.

    Parameters
    ----------
    cmd : SafeCmd
        The command about to be iterated.
    output : RunOutputOptions
        The options resolved for this iteration.
    context : ExecutionContext
        The context whose allowlist and encoding govern the run.

    Raises
    ------
    ForbiddenProgramError
        If ``cmd``'s program is not permitted by the active allowlist.
    ValueError
        If ``output`` redirects stdout, leaving nothing to iterate, or names a
        bounded capture whose encoding a truncated tail could not survive.
    """  # ruff: ignore[docstring-extraneous-exception] - both propagate from the helpers this calls
    _enforce_allowlist(cmd)
    _reject_redirected_lines_stdout(output)
    # ``lines`` yields text, so a bounded capture here is always a text capture
    # and always subject to the encoding rule.
    _reject_unsupported_bounded_captures(
        output.resolved_capture,
        encoding=context.encoding,
        capture_bytes=False,
    )


@dc.dataclass(frozen=True, slots=True)
class _LineIterationRequest:
    """The arguments ``SafeCmd.lines()`` was called with, already defaulted.

    They travel together because the preparation reads all of them at one
    moment and each feeds a different check: the options decide whether the
    command can be iterated at all, the context supplies the allowlist and
    encoding those checks apply, and stdin and timeout are resolved against it.
    """

    output: RunOutputOptions
    context: ExecutionContext
    stdin: StdinSource | None
    timeout: float | None


def _prepare_line_iteration(
    cmd: SafeCmd,
    request: _LineIterationRequest,
) -> tuple[_ExecutionTracking, cabc.Callable[[], _SubprocessExecution]]:
    """Check one command's options and freeze the run it would start.

    This is the whole of ``SafeCmd.lines()`` between resolving its arguments
    and handing the caller a stream: it refuses options that cannot be
    iterated, resolves stdin and the deadline, and builds the observation and
    execution factory the deferred pull re-authorizes.

    The deadline resolved here is a *provisional* one. The stream keeps the raw
    ``timeout`` argument and hands it to
    :func:`~cuprum._line_stream._refresh_line_stream_execution`, so a value
    read from a scope the caller has since left cannot follow the stream into
    the one that pulls it. Resolving it here still matters: it is what the
    observation and the pre-flight checks see, and it keeps the failure modes
    ``run()`` has — an unusable deadline is reported before any stream exists.

    Parameters
    ----------
    cmd : SafeCmd
        The command about to be iterated.
    request : _LineIterationRequest
        The caller's arguments, already defaulted where that applies.

    Returns
    -------
    tuple[_ExecutionTracking, collections.abc.Callable[[], _SubprocessExecution]]
        The hook bookkeeping the iteration travels with, and the factory that
        builds the execution bundle on demand.

    Raises
    ------
    ForbiddenProgramError
        If ``cmd``'s program is not permitted by the active allowlist.
    ValueError
        If ``request.output`` redirects stdout, or names a bounded capture
        whose encoding a truncated tail could not survive.
    """  # ruff: ignore[docstring-extraneous-exception] - both propagate from the checks this repeats
    out = request.output
    ctx = request.context
    # Provisional checks. They fail fast at construction, but the run is not
    # planned or spawned until the first ``__anext__``, and by then the active
    # scope may differ — so ``_iter_line_events`` repeats the allowlist check in
    # the consuming context before it does anything else.
    _reject_line_iteration_conflicts(cmd, out, ctx)
    state = _ExecutionState(
        context=ctx,
        output=out,
        stdin_data=_resolve_line_stdin_source(request.stdin, ctx, out),
        timeout=_resolve_timeout(timeout=request.timeout, context=ctx),
    )
    tracking = _line_iteration_tracking()
    observation = _prepare_execution_observation(cmd, state, tracking)
    return tracking, functools.partial(
        _build_subprocess_execution,
        cmd,
        state,
        observation=observation,
        sink_bracket=tracking.sink_bracket,
    )


def _line_iteration_tracking() -> _ExecutionTracking:
    """Build the hook bookkeeping one line iteration travels with.

    Line iteration never opens a presentation session: the line events are the
    caller's own consumption of the streams, so there is no adapter framing to
    bracket. The empty bracket keeps the required field satisfied.

    Returns
    -------
    _ExecutionTracking
        Tracking whose sink bracket owns nothing and whose pending-task list
        starts empty.
    """
    return _ExecutionTracking(
        execution_hooks=_collect_hooks(current_context()),
        pending_tasks=[],
        sink_bracket=_SinkBracket(None),
    )


def _reject_redirected_lines_stdout(output: RunOutputOptions) -> None:
    """Reject a stdout target where line iteration needs a parent-side pipe.

    ``RunOutputOptions`` cannot make this call on its own: the very same
    object is a valid argument to :meth:`SafeCmd.run`, which reads nothing
    back from stdout and is happy to send it to a file. The requirement is
    specific to line iteration, so the check belongs where the requirement is
    known.

    Parameters
    ----------
    output : RunOutputOptions
        The options about to drive a line iteration.

    Raises
    ------
    ValueError
        If ``output.stdout`` names anything other than a pipe. ``None`` means
        "unspecified", which resolves to a pipe for this path, so it is
        accepted.
    """
    if output.stdout is None or output.stdout.kind == "pipe":
        return
    msg = (
        f"SafeCmd.lines requires stdout to be a pipe, but output.stdout is "
        f"{output.stdout.kind!r}; there is no parent-side stream to iterate. "
        f"Use SafeCmd.run when stdout is redirected to a file or descriptor."
    )
    raise ValueError(msg)


def _resolve_line_stdin_source(
    stdin: StdinSource | None,
    context: ExecutionContext,
    output: RunOutputOptions,
) -> bytes | StdinStream | None:
    """Resolve line stdin and reject two competing sources."""
    resolved = _resolve_stdin_source(stdin, context)
    _reject_contested_stdin(output.stdin, has_source=resolved is not None)
    return resolved
