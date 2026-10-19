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

import typing as typ

from cuprum._bounded_encoding import _reject_unsupported_bounded_captures
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._pipeline_internals import _collect_hooks, _enforce_allowlist
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.context import current_context
from cuprum.sh.execution import _resolve_stdin_source
from cuprum.sh.stdio_rules import _reject_contested_stdin

if typ.TYPE_CHECKING:
    from cuprum.sh.execution import ExecutionContext, StdinSource, StdinStream
    from cuprum.sh.output import RunOutputOptions
    from cuprum.sh.safe_cmd import SafeCmd

__all__ = [
    "_line_iteration_tracking",
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
