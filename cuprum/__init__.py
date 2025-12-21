"""cuprum package.

Provides a typed programme catalogue system for managing curated, allowlisted
executables. Re-exports core types and the default catalogue for convenience.

Example:
>>> from cuprum import DEFAULT_CATALOGUE, ECHO
>>> entry = DEFAULT_CATALOGUE.lookup(ECHO)
>>> entry.project_name
'core-ops'

"""

from __future__ import annotations

from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    DEFAULT_PROJECTS,
    DOC_TOOL,
    DOCUMENTATION_PROJECT,
    ECHO,
    LS,
    ProgramCatalogue,
    ProgramEntry,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.context import (
    AfterHook,
    AllowRegistration,
    BeforeHook,
    CuprumContext,
    ExecHook,
    ForbiddenProgramError,
    HookRegistration,
    after,
    allow,
    before,
    current_context,
    get_context,
    observe,
    scoped,
)
from cuprum.events import ExecEvent
from cuprum.logging_hooks import LoggingHookRegistration, logging_hook
from cuprum.program import Program
from cuprum.sh import (
    CommandResult,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    SafeCmd,
    SafeCmdBuilder,
)

from . import sh

PACKAGE_NAME = "cuprum"

__all__ = [
    "CORE_OPS_PROJECT",
    "DEFAULT_CATALOGUE",
    "DEFAULT_PROJECTS",
    "DOCUMENTATION_PROJECT",
    "DOC_TOOL",
    "ECHO",
    "LS",
    "PACKAGE_NAME",
    "AfterHook",
    "AllowRegistration",
    "BeforeHook",
    "CommandResult",
    "CuprumContext",
    "ExecEvent",
    "ExecHook",
    "ExecutionContext",
    "ForbiddenProgramError",
    "HookRegistration",
    "LoggingHookRegistration",
    "Pipeline",
    "PipelineResult",
    "Program",
    "ProgramCatalogue",
    "ProgramEntry",
    "ProjectSettings",
    "SafeCmd",
    "SafeCmdBuilder",
    "UnknownProgramError",
    "after",
    "allow",
    "before",
    "current_context",
    "get_context",
    "logging_hook",
    "observe",
    "scoped",
    "sh",
]
