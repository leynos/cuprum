"""The byte-exact ``SafeCmd`` entry points.

Split from ``cuprum.sh.safe_cmd`` when the rebase onto ``main`` pushed that
module past the 400-line ceiling. The two methods are a coherent pair rather
than a slice of the text-mode surface: both return the child's own bytes, both
reject ``on_line`` for the same reason, and neither is reached by the other's
code path. Moving them together leaves ``safe_cmd`` about spawning and text
results.

``_BytesEntryPoints`` contributes ``SafeCmd.run_bytes`` and
``SafeCmd.run_bytes_sync`` the same way ``_OwnedStart`` contributes
``SafeCmd.start``: the class inherits the mixin, the public surface is
unchanged, and each method still resolves as an ordinary bound method.

This module deliberately omits ``from __future__ import annotations``, for the
same reason ``safe_cmd`` does: the methods are public signatures that
``cuprum.sh`` introspects with ``typing.get_type_hints``, so they are evaluated
eagerly. Nothing here names ``SafeCmd`` in an annotation — the methods' own
types are options, a result, and a context — so the mixin needs no import of
the class that inherits it, and no cycle exists to break.
"""

import asyncio

from cuprum._bytes_run import (
    _bytes_output,
    _require_bytes_command_result,
    _validate_bytes_output,
)
from cuprum._command_internals import _ExecutionState, _run_prepared_command
from cuprum._pipeline_internals import _enforce_allowlist
from cuprum._subprocess_context import _resolve_timeout
from cuprum.sh.execution import ExecutionContext, StdinInput
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import BytesCommandResult

__all__ = ["_BytesEntryPoints"]


class _BytesEntryPoints:
    """Mixin contributing ``SafeCmd.run_bytes`` and ``run_bytes_sync``.

    ``__slots__`` is declared empty rather than omitted so the un-slotted mixin
    cannot donate a ``__dict__`` to the slotted dataclass that inherits it, and
    the class is not a dataclass for the same reason as ``_OwnedStart``.
    """

    __slots__ = ()

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
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.

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
        UnicodeEncodeError
            If text stdin cannot be encoded by the execution context.

        """  # ruff: ignore[docstring-extraneous-exception] - ValueError and the public exceptions propagate through the bytes entry point
        return asyncio.run(
            self.run_bytes(
                output=output,
                timeout=timeout,
                context=context,
                stdin=stdin,
            ),
        )
