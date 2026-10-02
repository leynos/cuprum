"""Shared internals for the byte-exact command and pipeline entry points.

The ``run_bytes()`` entry points on ``SafeCmd`` and ``Pipeline`` are not
separate runners. A binary run is one of the ordinary runs — same allowlist
enforcement, same stdin resolution, same timeout precedence, same
finalization — carrying one extra fact: its captured streams must not be
decoded. That fact travels as ``capture_bytes`` on the run's resolved state
and, from there, on the stream configuration the drains read.

What both entry points owe regardless of which of them is called is a decision
about the options they were handed, and that decision lives here:

- :func:`_validate_bytes_output` rejects the one combination bytes mode does
  not offer — a caller-supplied line observer — before anything is spawned;
- :func:`_bytes_output` normalizes the optional options object a caller may
  omit.

The two result-bearing helpers serve the same split on the way out:
:func:`_require_command_result` and :func:`_require_pipeline_result` re-take the
narrower class a text-mode entry point promised, and
:func:`_require_bytes_pipeline_result` the byte-exact one. A shared runner
returns either class, and the narrowing keeps the mismatch out of the caller
rather than asserting it away.

Keeping them together is what stops the command and pipeline paths from
growing two slightly different answers to "what may a binary run be asked
for". The public entry points that ask the question live in
:mod:`cuprum.sh.safe_cmd` and :mod:`cuprum.sh.pipeline`, which is why this
module imports its options from :mod:`cuprum.sh.output` rather than from the
package facade that those modules belong to.
"""

from __future__ import annotations

import typing as typ

# Sourced from ``cuprum.sh.output`` rather than ``cuprum.sh``: the package
# facade imports the mixin module, which imports this one, so reaching back
# through the facade would close a cycle at import time.
from cuprum.sh.output import RunOutputOptions
from cuprum.sh.results import (
    BytesCommandResult,
    BytesPipelineResult,
    CommandResult,
    PipelineResult,
)

if typ.TYPE_CHECKING:
    from cuprum._result_types import _AnyCommandResult, _AnyPipelineResult


def _require_command_result(result: _AnyCommandResult) -> CommandResult:
    """Narrow a shared runner's result to the text-mode class it promised.

    The command path runs through one implementation for both modes, so its
    return type is the union; a text-mode entry point narrowed it to
    ``CommandResult`` in its own ``Returns`` section and re-takes that
    guarantee here. A byte-exact result arriving on the text path would be a
    contradiction, and saying so beats handing the caller an object whose
    fields are not the type its signature claims.

    Returns
    -------
    CommandResult
        The same result, narrowed to the text class.

    Raises
    ------
    TypeError
        If a byte-exact result reached a text-mode entry point.

    """
    if not isinstance(result, CommandResult):
        msg = "text-mode run produced a byte-exact result"
        raise TypeError(msg)
    return result


def _require_bytes_command_result(result: _AnyCommandResult) -> BytesCommandResult:
    """Narrow a shared runner's result to the byte-exact class.

    The counterpart of :func:`_require_command_result` for the binary entry
    point. ``BytesCommandResult`` does not subclass ``CommandResult``, so the
    guarantee has to be re-taken here rather than left to the caller.

    Returns
    -------
    BytesCommandResult
        The same result, narrowed to the byte-exact class.

    Raises
    ------
    TypeError
        If a text result reached the byte-exact entry point.

    """
    if not isinstance(result, BytesCommandResult):
        msg = "byte-exact run produced a text result"
        raise TypeError(msg)
    return result


def _require_pipeline_result(result: _AnyPipelineResult) -> PipelineResult:
    """Narrow a shared runner's pipeline result to the text-mode class.

    The counterpart of :func:`_require_command_result` for pipelines.

    Returns
    -------
    PipelineResult
        The same result, narrowed to the text class.

    Raises
    ------
    TypeError
        If a byte-exact result reached a text-mode entry point.

    """
    if not isinstance(result, PipelineResult):
        msg = "text-mode pipeline produced a byte-exact result"
        raise TypeError(msg)
    return result


def _require_bytes_pipeline_result(
    result: _AnyPipelineResult,
) -> BytesPipelineResult:
    """Narrow a shared runner's pipeline result to the byte-exact class.

    ``BytesPipelineResult`` does not subclass ``PipelineResult``, so the
    byte-exact entry point has to re-take its own class the same way.

    Returns
    -------
    BytesPipelineResult
        The same result, narrowed to the byte-exact class.

    Raises
    ------
    TypeError
        If a text result reached the byte-exact entry point.

    """
    if not isinstance(result, BytesPipelineResult):
        msg = "byte-exact pipeline produced a text result"
        raise TypeError(msg)
    return result


def _bytes_output(output: RunOutputOptions | None) -> RunOutputOptions:
    """Return the options a bytes-mode call runs with, defaulting when omitted.

    Returns
    -------
    RunOutputOptions
        The caller's options, or the all-defaults options object.

    """
    return output or RunOutputOptions()


def _validate_bytes_output(options: RunOutputOptions) -> None:
    """Reject the options combinations a byte-exact run cannot honour.

    The check is deliberately narrow. Everything else ``RunOutputOptions``
    offers — capture, both echo shorthands, the sink, the idle options — is
    meaningful for binary output, so bytes mode keeps them; refusing the whole
    options object would take away far more than binary output costs. Line
    observation is the single exception, and it is refused rather than
    downgraded: ``on_line`` carries decoded text while the run's capture is
    byte-exact, so honouring it would give one stream two contradictory
    contracts. A callback that silently stopped firing would be harder to
    notice than a rejected call.

    This is a policy decision, not a limit of the drain, which serves both
    modes at once and still delivers decoded lines to registered observe
    hooks. Only the caller-supplied ``on_line`` is refused; internal
    observation is unaffected.

    Called before anything is spawned, so a caller that combines the two
    learns about it from a ``ValueError`` rather than from a child that ran
    to completion and reported nothing.

    Raises
    ------
    ValueError
        If ``options.on_line`` is set.

    """
    if options.on_line is not None:
        msg = (
            "on_line observes decoded text and cannot be combined with byte-exact "
            "capture; drop on_line, or use the text-mode entry point"
        )
        raise ValueError(msg)


__all__ = [
    "_bytes_output",
    "_require_bytes_command_result",
    "_require_bytes_pipeline_result",
    "_require_command_result",
    "_require_pipeline_result",
    "_validate_bytes_output",
]
