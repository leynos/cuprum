"""``SafeCmd`` — the typed, immutable curated command for ``cuprum.sh``.

``SafeCmd`` is the execution primitive: a validated command that runs one
program, with or without streams. Composition lives in
:mod:`cuprum.sh.pipeline`, which builds pipelines out of commands. The two
modules reference each other, so ``Pipeline`` is bound by a module-level import
at the *bottom* of this file, once ``SafeCmd`` exists for the pipeline module
to import. The ``cuprum.sh`` package re-exports both.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# reference to ``Pipeline`` inside ``SafeCmd``'s own class body is quoted; the
# module-level import that binds it sits at the bottom of this file.
import asyncio
import collections.abc as cabc
import dataclasses as dc
import typing as typ

from cuprum._command_internals import (
    _build_subprocess_execution,
    _ExecutionState,
    _prepare_execution_observation,
    _run_prepared_command,
)
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._line_iteration import LineStream, _iter_line_events
from cuprum._pipeline_internals import (
    _collect_hooks,
    _enforce_allowlist,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum._subprocess_context import _resolve_timeout
from cuprum.catalogue import ProjectSettings
from cuprum.context import current_context
from cuprum.program import Program
from cuprum.sh.execution import (
    ExecutionContext,
    StdinSource,
    _resolve_stdin_source,
)
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import CommandResult

if typ.TYPE_CHECKING:
    from cuprum.sh.pipeline import Pipeline

type SafeCmdBuilder = cabc.Callable[..., SafeCmd]


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


__all__ = [
    "SafeCmd",
    "SafeCmdBuilder",
]


@dc.dataclass(frozen=True, slots=True)
class SafeCmd:
    """Typed representation of a curated command ready for execution."""

    program: Program

    argv: tuple[str, ...]

    project: ProjectSettings

    __weakref__: object = dc.field(
        init=False,
        repr=False,
        hash=False,
        compare=False,
    )

    @property
    def argv_with_program(self) -> tuple[str, ...]:
        """The program name followed by this command's arguments.

        Returns
        -------
        tuple[str, ...]
            An argument vector whose first item is ``str(program)``.
        """
        return (str(self.program), *self.argv)

    def __or__(self, other: "SafeCmd | Pipeline") -> "Pipeline":
        """Compose this command with another stage, producing a Pipeline."""
        return Pipeline.concat(self, other)

    async def run(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult:
        """Execute the command asynchronously with predictable cancellation.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. Its 64 KiB default bounds each mirrored
            line without affecting capture; set ``max_echo_line_bytes=None``
            for unbounded mirroring.
        timeout : float | None, default=None
            Maximum execution time in seconds. An explicit value overrides the
            timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | StdinStream | None, default=None
            Optional stdin source. ``StdinInput`` supplies one complete bytes
            or text payload; ``StdinStream`` supplies an async producer whose
            chunks are pulled one at a time, written, and drained before the
            next is pulled, so peak memory is bounded by the largest chunk.
            ``None`` inherits the parent's stdin.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            ``output.capture`` is true.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        StdinSourceError
            If a ``StdinStream`` producer or its encoder fails. The child is
            terminated before this is raised.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through execution helpers
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_source = _resolve_stdin_source(stdin, ctx)
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        return await _run_prepared_command(
            self,
            _ExecutionState(
                context=ctx,
                output=out,
                stdin_data=stdin_source,
                timeout=effective_timeout,
            ),
        )

    def lines(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> LineStream:
        """Iterate the command's output lines as they arrive.

        Line events are delivered in arrival order per stream, stamped with
        monotonic seconds since the command started. Capture and echo stay
        governed by *output* independently: iterating lines does not disable
        either unless the caller asks.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling.
        timeout:
            Optional wall-clock timeout in seconds; ``None`` disables timeouts.
            Expiry terminates the subprocess exactly as ``run()`` does.
        context:
            Optional execution settings such as env, cwd, and cancel grace.
        stdin:
            Optional stdin source, as for :meth:`run`.

        Returns
        -------
        LineStream
            An async iterator of ``LineEvent`` whose ``result`` attribute
            holds the final ``CommandResult`` once iteration completes.

        Raises
        ------
        ForbiddenProgramError
            If the program is not permitted by the active context allowlist.
        TimeoutExpired
            If *timeout* elapses before the command completes.
        UnicodeEncodeError
            If ``stdin`` text cannot be encoded with the context's encoding.
        StdinSourceError
            If a ``StdinStream`` producer or its encoder fails.
        ValueError
            If *output* redirects stdout: line iteration reads stdout through
            a parent-side pipe, so a redirected stdout has nothing to iterate.
            Use :meth:`run` when stdout goes to a file or descriptor.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and stdin encode
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        _reject_redirected_lines_stdout(out)
        stdin_source = _resolve_stdin_source(stdin, ctx)
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        tracking = _ExecutionTracking(
            execution_hooks=_collect_hooks(current_context()),
            pending_tasks=[],
            # Line iteration never opens a presentation session: the line
            # events are the caller's own consumption of the streams, so there
            # is no adapter framing to bracket. The empty bracket keeps the
            # required field satisfied.
            sink_bracket=_SinkBracket(None),
        )
        observation = _prepare_execution_observation(self, ctx, tracking, out)

        return LineStream(
            _iter_line_events(
                _build_subprocess_execution(
                    self,
                    _ExecutionState(
                        context=ctx,
                        output=out,
                        stdin_data=stdin_source,
                        timeout=effective_timeout,
                    ),
                    observation=observation,
                ),
                tracking,
            ),
        )

    def run_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult:
        """Execute the command synchronously.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. The default limits each mirrored line to
            64 KiB; ``max_echo_line_bytes=None`` restores unbounded echoing
            while preserving the same capture contract.
        timeout : float | None, default=None
            Maximum execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | StdinStream | None, default=None
            Optional stdin source, as for :meth:`run`.

        Returns
        -------
        CommandResult
            The command outcome, including complete captured streams when
            enabled.

        Raises
        ------
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.
        StdinSourceError
            If a ``StdinStream`` producer or its encoder fails.
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        return asyncio.run(
            self.run(output=output, timeout=timeout, context=context, stdin=stdin),
        )


# Loaded last, and bound in this module's namespace, for two reasons that
# disagree with the usual ordering rule.
#
# ``SafeCmd.__or__`` is annotated with ``Pipeline``, and ``cuprum.sh``
# introspects public signatures with ``typing.get_type_hints``, which evaluates
# a quoted annotation against this module's namespace alone — a function-local
# import inside ``__or__`` would fire too late for that. Binding it here keeps
# ``get_type_hints(SafeCmd.__or__)`` resolvable without a caller having to
# compose anything first.
#
# The position also keeps the cycle open rather than closed:
# ``cuprum.sh.pipeline`` imports ``SafeCmd`` at *its* module scope. By the time
# this line runs, ``SafeCmd`` is bound, so the pipeline module builds normally;
# at the top of the file, ``pipeline`` would have been asked to import a
# ``SafeCmd`` that did not exist yet.
from cuprum.sh.pipeline import (  # ruff: ignore[module-import-not-at-top-of-file]
    Pipeline,
)
