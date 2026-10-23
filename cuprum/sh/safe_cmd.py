"""The single-command execution primitive.

Split out of the former ``cuprum/sh/safe_cmd.py`` module so each of the two
execution primitives owns a module: their byte-exact entry points are ordinary
methods of the class they extend, and keeping the class next to those methods
is what lets the methods annotate ``self`` implicitly rather than naming the
class in a string that only ``TYPE_CHECKING`` could resolve. ``Pipeline``
lives in ``cuprum/sh/pipeline.py``, and ``SafeCmdBuilder`` — the callable
contract ``sh.make`` returns — lives in ``cuprum/sh/builder.py``, which
imports ``SafeCmd`` from here; the reverse import would close that cycle.

The command and pipeline modules still import each other — a command
composes into a pipeline and a pipeline is built from commands — so the cycle
between them is broken the same way ``cuprum.sh`` breaks its own: each side
defers the import it needs to the point of use. ``cuprum/sh/__init__.py``
re-exports the classes unchanged, so importers of ``cuprum.sh`` see the same
objects they always did. ``SafeCmd.start`` is contributed by ``_OwnedStart``
in :mod:`cuprum._process_handle.owned_start`, which this class inherits; the
handle path resolves its own run inputs, so it lives with the handle package.
"""

# No ``from __future__ import annotations`` here: the public signatures are
# introspected with ``typing.get_type_hints``, so annotations are evaluated
# eagerly and every name they use is a genuine runtime import. Only the forward
# reference to ``Pipeline`` inside ``SafeCmd``'s own class body is quoted; the
# module-level import that binds it sits at the bottom of this file.
import asyncio
import dataclasses as dc
import typing  # ruff: ignore[unconventional-import-alias]  # Interrogate recognizes only typing.overload.

from cuprum._bytes_run import _require_command_result
from cuprum._command_internals import (
    _ExecutionState,
    _run_prepared_command,
)
from cuprum._line_iteration import LineStream, _iter_line_events
from cuprum._pipeline_internals import _enforce_allowlist
from cuprum._process_handle.owned_start import _OwnedStart
from cuprum._subprocess_context import _resolve_timeout
from cuprum.catalogue import ProjectSettings
from cuprum.program import Program
from cuprum.sh.bytes_entry_points import _BytesEntryPoints
from cuprum.sh.execution import (
    ExecutionContext,
    StdinSource,
    _resolve_stdin_source,
)
from cuprum.sh.line_options import (
    # Re-exported under this module's name as well as imported for use here.
    # This module is where the line-iteration path has always been read from;
    # re-importing the helpers elsewhere would give a test double a second
    # binding that no production call path resolves. The ``as`` form marks the
    # names this module no longer calls but must keep binding.
    _line_iteration_tracking as _line_iteration_tracking,
)
from cuprum.sh.line_options import _LineIterationRequest, _prepare_line_iteration
from cuprum.sh.line_options import (
    _reject_redirected_lines_stdout as _reject_redirected_lines_stdout,
)
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import CapturedResult, CommandResult

__all__ = [
    "SafeCmd",
]


@dc.dataclass(frozen=True, slots=True)
class SafeCmd(_OwnedStart, _BytesEntryPoints):
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

    @typing.overload
    async def run(
        self,
        *,
        output: None = None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CapturedResult: ...

    @typing.overload
    async def run(
        self,
        *,
        output: RunOutputOptions,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult: ...

    @typing.overload
    async def run(
        self,
        *,
        output: RunOutputOptions | None,
        timeout: float | None = None,  # ruff: ignore[async-function-with-timeout]  # ExecutionContext also supplies the timeout.
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult: ...

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
            next is pulled. That backpressure limits how far *ahead* the
            producer runs — it does not stop the writer pulling ahead of the
            child's reads, because ``drain()`` returns at the transport's
            low-water mark rather than on an empty buffer, and the OS pipe
            holds bytes of its own. The payload is never held whole, and
            retained memory is roughly the chunk currently being written plus
            those buffers, so yield bounded-size chunks when that matters.
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
        return _require_command_result(
            await _run_prepared_command(
                self,
                _ExecutionState(
                    context=ctx,
                    output=out,
                    stdin_data=stdin_source,
                    timeout=effective_timeout,
                ),
            )
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

        Line events arrive in order per stream, stamped with monotonic seconds
        since the command started. Capture and echo stay governed by *output*
        independently: iterating lines disables neither unless asked.

        The allowlist and the deadline are checked twice: here, when the lazy
        stream is prepared, and again in the context that pulls the first
        event. The second check is what makes the deferred spawn obey a scope
        entered after the stream was built — constructing a stream is not
        permission to run the program.

        Parameters
        ----------
        output:
            Optional ``RunOutputOptions`` controlling stdout/stderr handling.
        timeout:
            Optional wall-clock timeout. Resolved again when iteration starts:
            an explicit value here wins, then ``context.timeout``, then the
            consuming scope's timeout; ``None`` inherits those fallbacks rather
            than disabling timeouts. Expiry ends the subprocess as ``run()``
            does.
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
            If the allowlist forbids the program, either at creation or when
            the first event is requested.
        TimeoutExpired
            If the effective timeout elapses before the command completes.
        UnicodeEncodeError
            If ``stdin`` text cannot be encoded with the context's encoding.
        StdinSourceError
            If a ``StdinStream`` producer or its encoder fails.
        ValueError
            If *output* redirects stdout or names a bounded capture whose
            encoding a truncated tail could not survive: line iteration reads
            stdout through a parent-side pipe. Use :meth:`run` when stdout goes
            to a file or descriptor.
        """  # ruff: ignore[docstring-extraneous-exception] - all propagate from allowlist, timeout, stdin encode, and the option checks
        tracking, execution_factory = _prepare_line_iteration(
            self,
            _LineIterationRequest(
                output=output or RunOutputOptions(),
                context=context or ExecutionContext(),
                stdin=stdin,
                timeout=timeout,
            ),
        )
        # The raw timeout travels with the generator, not the value
        # ``_prepare_line_iteration`` resolved: re-resolving that value would
        # keep a timeout from a scope the caller has since left. ``None`` must
        # stay ``None`` so the consuming scope's default can apply.
        return LineStream(
            _iter_line_events(
                execution_factory,
                tracking,
                requested_timeout=timeout,
            )
        )

    @typing.overload
    def run_sync(
        self,
        *,
        output: None = None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CapturedResult: ...

    @typing.overload
    def run_sync(
        self,
        *,
        output: RunOutputOptions,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult: ...

    @typing.overload
    def run_sync(
        self,
        *,
        output: RunOutputOptions | None,
        timeout: float | None = None,
        context: ExecutionContext | None = None,
        stdin: StdinSource | None = None,
    ) -> CommandResult: ...

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


# Imported at the foot of the module, after ``SafeCmd`` exists, to break the
# mutual dependency with ``cuprum.sh.pipeline``: that module imports this one
# for its ``SafeCmd``, so importing it at the head would leave it reading a
# half-built module. ``__or__`` needs the real class rather than a postponed
# name, because ``Pipeline.concat`` is called on it — and a function-local
# import would fire too late for ``typing.get_type_hints``, which
# ``cuprum.sh`` uses to resolve ``__or__``'s quoted annotation against this
# module's namespace alone.
from cuprum.sh.pipeline import (  # ruff: ignore[module-import-not-at-top-of-file]  # deferred to break the cuprum.sh.pipeline dependency cycle
    Pipeline,
)
