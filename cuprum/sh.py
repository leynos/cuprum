"""Safe command construction and execution facade for curated programs.

This module focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.

The implementation is split across ``cuprum._sh_*`` modules grouped by
cohesive responsibility (argv construction, execution context/stdin/timeout
types, result types, output options, and the ``SafeCmd``/``Pipeline``
execution primitives) to keep every module within the project's file-size
ceiling. Every name previously defined or imported here remains importable
from this module with the same object identity; this module is the stable
public and internal entry point.
"""

from __future__ import annotations

from cuprum._command_internals import (
    _build_subprocess_execution as _build_subprocess_execution,
)
from cuprum._command_internals import _ExecutionState as _ExecutionState
from cuprum._command_internals import (
    _prepare_execution_observation as _prepare_execution_observation,
)
from cuprum._command_internals import _run_prepared_command as _run_prepared_command
from cuprum._constants import DEFAULT_ECHO_MAX_LINE_BYTES as DEFAULT_ECHO_MAX_LINE_BYTES
from cuprum._execution_tracking import _ExecutionTracking as _ExecutionTracking
from cuprum._idle_heartbeat import _validate_idle_options as _validate_idle_options
from cuprum._line_iteration import LineStream
from cuprum._line_iteration import _iter_line_events as _iter_line_events
from cuprum._pipeline_config import _prepare_pipeline_config as _prepare_pipeline_config
from cuprum._pipeline_internals import _MIN_PIPELINE_STAGES as _MIN_PIPELINE_STAGES
from cuprum._pipeline_internals import _collect_hooks as _collect_hooks
from cuprum._pipeline_internals import _enforce_allowlist as _enforce_allowlist
from cuprum._pipeline_internals import _run_pipeline as _run_pipeline
from cuprum._sh_argv import Path as Path
from cuprum._sh_argv import _ArgValue, build_argv
from cuprum._sh_argv import _serialize_kwargs as _serialize_kwargs
from cuprum._sh_argv import _stringify_arg as _stringify_arg
from cuprum._sh_context import _DEFAULT_CANCEL_GRACE as _DEFAULT_CANCEL_GRACE
from cuprum._sh_context import _DEFAULT_ENCODING as _DEFAULT_ENCODING
from cuprum._sh_context import _DEFAULT_ERROR_HANDLING as _DEFAULT_ERROR_HANDLING
from cuprum._sh_context import (
    _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE as _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
)
from cuprum._sh_context import ExecutionContext, StdinInput, TimeoutExpired
from cuprum._sh_context import _CwdType as _CwdType
from cuprum._sh_context import _EnvMapping as _EnvMapping
from cuprum._sh_context import cabc as cabc
from cuprum._sh_output import IOOptions, RunOutputOptions
from cuprum._sh_output import _DeprecatedOutputFlags as _DeprecatedOutputFlags
from cuprum._sh_output import _resolve_pipeline_output as _resolve_pipeline_output
from cuprum._sh_output import _validate_convenience_flags as _validate_convenience_flags
from cuprum._sh_output import sinks as sinks
from cuprum._sh_output import typ as typ
from cuprum._sh_output import warnings as warnings
from cuprum._sh_results import CommandResult, PipelineResult
from cuprum._sh_results import dc as dc
from cuprum._sh_safe_cmd import Pipeline, SafeCmd, SafeCmdBuilder
from cuprum._sh_safe_cmd import asyncio as asyncio
from cuprum._sink_lifecycle import _outcome_for_error as _outcome_for_error
from cuprum._sink_lifecycle import _SinkBracket as _SinkBracket
from cuprum._subprocess_context import _resolve_timeout as _resolve_timeout
from cuprum.catalogue import DEFAULT_CATALOGUE, ProgramCatalogue
from cuprum.catalogue import ProjectSettings as ProjectSettings
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import _validate_timeout as _validate_timeout
from cuprum.context import current_context as current_context
from cuprum.context import observe as observe
from cuprum.context import scoped as scoped
from cuprum.echo_events import RelayFallback as RelayFallback

# Public annotations use ``Program``. Keep it in module globals so
# ``typing.get_type_hints`` can resolve the postponed public annotations.
from cuprum.program import (
    Program,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)
from cuprum.sinks import GitHubActionsSink as GitHubActionsSink


def make(
    program: Program,
    *,
    catalogue: ProgramCatalogue = DEFAULT_CATALOGUE,
) -> SafeCmdBuilder:
    """Build a callable that produces ``SafeCmd`` instances for ``program``.

    Parameters
    ----------
    program : Program
        The program the built ``SafeCmd`` instances invoke; it must exist in
        ``catalogue``.
    catalogue : ProgramCatalogue
        The catalogue used to validate ``program`` and resolve its entry.

    Returns
    -------
    SafeCmdBuilder
        A callable that builds ``SafeCmd`` instances for ``program``.

    Raises
    ------
    UnknownProgramError
        If ``program`` does not exist in ``catalogue``.
    """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from catalogue.lookup
    entry = catalogue.lookup(program)

    def builder(*args: _ArgValue, **kwargs: _ArgValue) -> SafeCmd:
        """Coerce ``args``/``kwargs`` into a ``SafeCmd`` for the program."""
        argv = build_argv(*args, **kwargs)
        return SafeCmd(program=entry.program, argv=argv, project=entry.project)

    return builder


__all__ = [
    "CommandResult",
    "ExecutionContext",
    "IOOptions",
    "LineStream",
    "Pipeline",
    "PipelineResult",
    "RunOutputOptions",
    "SafeCmd",
    "SafeCmdBuilder",
    "StdinInput",
    "TimeoutExpired",
    "UnknownProgramError",
    "build_argv",
    "make",
    "observe",
    "scoped",
]
