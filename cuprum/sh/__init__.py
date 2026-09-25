"""Safe command construction and execution facade for curated programs.

This package focuses on the typed core: building ``SafeCmd`` instances from
curated ``Program`` values and providing a minimal async runtime for executing
them with predictable semantics.

The implementation is split across submodules by responsibility:

- ``argv`` builds argument vectors and publishes ``ArgValue``.
- ``builder`` holds the ``SafeCmdBuilder`` callable contract.
- ``execution`` holds the execution context, stdin, and timeout types.
- ``results`` holds ``CommandResult`` and ``PipelineResult``.
- ``output`` holds ``RunOutputOptions`` and ``IOOptions``.
- ``safe_cmd`` holds the ``SafeCmd`` and ``Pipeline`` execution primitives.
- ``factory`` holds the ``make`` builder factory.

Every name previously defined or imported by the former ``cuprum/sh.py``
module remains importable from ``cuprum.sh`` with the same object identity;
the package is the stable public and internal entry point.
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
from cuprum._sink_lifecycle import _outcome_for_error as _outcome_for_error
from cuprum._sink_lifecycle import _SinkBracket as _SinkBracket
from cuprum._subprocess_context import _resolve_timeout as _resolve_timeout
from cuprum.catalogue import DEFAULT_CATALOGUE as DEFAULT_CATALOGUE
from cuprum.catalogue import ProgramCatalogue as ProgramCatalogue
from cuprum.catalogue import ProjectSettings as ProjectSettings
from cuprum.catalogue import UnknownProgramError as UnknownProgramError
from cuprum.context import _validate_timeout as _validate_timeout
from cuprum.context import current_context as current_context
from cuprum.context import observe as observe
from cuprum.context import scoped as scoped
from cuprum.echo_events import RelayFallback as RelayFallback
from cuprum.program import Program as Program
from cuprum.sh.argv import ArgValue as ArgValue
from cuprum.sh.argv import Path as Path
from cuprum.sh.argv import _serialize_kwargs as _serialize_kwargs
from cuprum.sh.argv import _stringify_arg as _stringify_arg
from cuprum.sh.argv import build_argv
from cuprum.sh.builder import SafeCmdBuilder as SafeCmdBuilder
from cuprum.sh.execution import _DEFAULT_CANCEL_GRACE as _DEFAULT_CANCEL_GRACE
from cuprum.sh.execution import _DEFAULT_ENCODING as _DEFAULT_ENCODING
from cuprum.sh.execution import _DEFAULT_ERROR_HANDLING as _DEFAULT_ERROR_HANDLING
from cuprum.sh.execution import (
    _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE as _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE,
)
from cuprum.sh.execution import ExecutionContext, StdinInput, TimeoutExpired
from cuprum.sh.execution import _CwdType as _CwdType
from cuprum.sh.execution import _EnvMapping as _EnvMapping
from cuprum.sh.execution import cabc as cabc
from cuprum.sh.factory import make
from cuprum.sh.output import IOOptions, RunOutputOptions
from cuprum.sh.output import _DeprecatedOutputFlags as _DeprecatedOutputFlags
from cuprum.sh.output import _resolve_pipeline_output as _resolve_pipeline_output
from cuprum.sh.output import _validate_convenience_flags as _validate_convenience_flags
from cuprum.sh.output import sinks as sinks
from cuprum.sh.output import typ as typ
from cuprum.sh.output import warnings as warnings
from cuprum.sh.results import CommandResult, PipelineResult
from cuprum.sh.results import dc as dc
from cuprum.sh.safe_cmd import Pipeline, SafeCmd
from cuprum.sh.safe_cmd import asyncio as asyncio
from cuprum.sinks import GitHubActionsSink as GitHubActionsSink

__all__ = [
    "ArgValue",
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
