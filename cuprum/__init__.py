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

from cuprum._constants import PACKAGE_NAME
from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    DEFAULT_PROJECTS,
    DOC_TOOL,
    DOCUMENTATION_PROJECT,
    ECHO,
    GIT,
    LS,
    RSYNC,
    TAR,
    ProgramCatalogue,
    ProgramEntry,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.concurrent import (
    ConcurrentConfig,
    ConcurrentResult,
    run_concurrent,
    run_concurrent_sync,
)
from cuprum.context import (
    AfterHook,
    AllowRegistration,
    BeforeHook,
    CuprumContext,
    ExecHook,
    ForbiddenProgramError,
    HookRegistration,
    ScopeConfig,
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
from cuprum.rust import is_rust_available
from cuprum.sh import (
    CommandResult,
    ExecutionContext,
    Pipeline,
    PipelineResult,
    SafeCmd,
    SafeCmdBuilder,
    TimeoutExpired,
)

from . import builders, sh

__all__ = [
    "CORE_OPS_PROJECT",
    "DEFAULT_CATALOGUE",
    "DEFAULT_PROJECTS",
    "DOCUMENTATION_PROJECT",
    "DOC_TOOL",
    "ECHO",
    "GIT",
    "LS",
    "PACKAGE_NAME",
    "RSYNC",
    "TAR",
    "AfterHook",
    "AllowRegistration",
    "BeforeHook",
    "CommandResult",
    "ConcurrentConfig",
    "ConcurrentResult",
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
    "ScopeConfig",
    "TimeoutExpired",
    "UnknownProgramError",
    "after",
    "allow",
    "before",
    "builders",
    "current_context",
    "get_context",
    "is_rust_available",
    "logging_hook",
    "observe",
    "run_concurrent",
    "run_concurrent_sync",
    "scoped",
    "sh",
]
