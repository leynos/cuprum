"""The single-command execution primitive.

Split out of the former ``cuprum/sh/safe_cmd.py`` module so each of the two
execution primitives owns a module: their byte-exact entry points are ordinary
methods of the class they extend, and keeping the class next to those methods
is what lets the methods annotate ``self`` implicitly rather than naming the
class in a string that only ``TYPE_CHECKING`` could resolve. The modules still
import each other — a command composes into a pipeline and a pipeline is built
from commands — so the cycle between them is broken the same way ``cuprum.sh``
breaks its own: each side defers the import it needs to the point of use.

``cuprum/sh/__init__.py`` re-exports both classes unchanged, so importers of
``cuprum.sh`` see the same objects they always did.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# references to ``SafeCmd`` and ``Pipeline`` inside the class bodies are quoted.
import asyncio
import collections.abc as cabc
import dataclasses as dc

from cuprum._bytes_run import (
    _bytes_output,
    _require_bytes_command_result,
    _require_command_result,
    _validate_bytes_output,
)
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
from cuprum.sh.execution import ExecutionContext, StdinInput
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import BytesCommandResult, CommandResult

type SafeCmdBuilder = cabc.Callable[..., SafeCmd]

__all__ = [
    "Pipeline",
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
        stdin: StdinInput | None = None,
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
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

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
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through execution helpers
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        return _require_command_result(
            await _run_prepared_command(
                self,
                _ExecutionState(
                    context=ctx,
                    output=out,
                    stdin_data=stdin_data,
                    timeout=effective_timeout,
                ),
            )
        )

    async def run_bytes(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> BytesCommandResult:
        """Execute the command, capturing its output as bytes.

        The run itself is the one ``run()`` performs; only what happens to the
        captured streams differs. Bytes are returned exactly as the child wrote
        them — no decoding, no replacement characters — so values that are not
        valid text survive the round trip.

        Echoing, sink presentation, and idle reporting all behave as they do
        in text mode. A sink exposing a ``buffer`` receives the child's own
        bytes; a text-only sink decodes them for display only, and a mirror it
        cannot render is disabled without disturbing what was captured.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. ``on_line`` is rejected: bytes mode
            returns the child's own bytes, so a callback carrying decoded text
            would be a second, contradictory contract for the same stream. Use
            the text-mode entry point when lines are wanted.
        timeout : float | None, default=None
            Maximum execution time in seconds. An explicit value overrides the
            timeout in ``context``.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        BytesCommandResult
            The command outcome, carrying ``bytes`` for each captured stream
            when ``output.capture`` is true and ``None`` for each stream that
            was not captured.

        Raises
        ------
        ValueError
            If ``output.on_line`` is set.
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.

        """  # ruff: ignore[docstring-extraneous-exception] - ValueError and the public exceptions propagate through the bytes entry point
        out = _bytes_output(output)
        _validate_bytes_output(out)
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
        effective_timeout = _resolve_timeout(timeout=timeout, context=context)
        return _require_bytes_command_result(
            await _run_prepared_command(
                self,
                _ExecutionState(
                    context=ctx,
                    output=out,
                    stdin_data=stdin_data,
                    timeout=effective_timeout,
                    capture_bytes=True,
                ),
            )
        )

    def run_bytes_sync(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
    ) -> BytesCommandResult:
        """Execute the command synchronously, capturing its output as bytes.

        Parameters
        ----------
        output : RunOutputOptions | None, default=None
            Capture and echo settings. ``on_line`` is rejected: bytes mode
            returns the child's own bytes, so a callback carrying decoded text
            would be a second, contradictory contract for the same stream. Use
            the text-mode entry point when lines are wanted.
        timeout : float | None, default=None
            Maximum execution time in seconds.
        context : ExecutionContext | None, default=None
            Execution settings, including echo sinks and text encoding.
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

        Returns
        -------
        BytesCommandResult
            The command outcome, carrying ``bytes`` for each captured stream
            when capture is enabled and ``None`` for each stream that was not
            captured.

        Raises
        ------
        ValueError
            If ``output.on_line`` is set.
        PermissionError
            If the command is not allowed by the active scope.
        TimeoutError
            If execution exceeds the effective timeout.

        """  # ruff: ignore[docstring-extraneous-exception] - ValueError and the public exceptions propagate through the bytes entry point
        return asyncio.run(
            self.run_bytes(
                output=output,
                timeout=timeout,
                context=context,
                stdin=stdin,
            ),
        )

    def lines(
        self,
        *,
        output: RunOutputOptions | None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinInput | None = None,
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
            Optional ``StdinInput`` data to feed to the subprocess.

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
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, and stdin encode
        out = output or RunOutputOptions()
        ctx = context or ExecutionContext()
        _enforce_allowlist(self)
        stdin_data = stdin.resolve(ctx) if stdin is not None else None
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
                        stdin_data=stdin_data,
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
        stdin: StdinInput | None = None,
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
        stdin : StdinInput | None, default=None
            Optional bytes or text supplied to the child process's stdin.

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
        """  # ruff: ignore[docstring-extraneous-exception] - public exceptions propagate through run()
        return asyncio.run(
            self.run(output=output, timeout=timeout, context=context, stdin=stdin),
        )


# Imported at the foot of the module, after both classes exist, to break the
# mutual dependency with ``cuprum.sh.pipeline``: that module imports this one
# for its ``SafeCmd`` annotation, so importing it at the head would leave it
# reading a half-built module. ``__or__`` needs the real class rather than a
# postponed name, because ``Pipeline.concat`` is called on it.
from cuprum.sh.pipeline import (  # ruff: ignore[module-import-not-at-top-of-file]  # deferred to break the cuprum.sh.pipeline dependency cycle
    Pipeline,
)
