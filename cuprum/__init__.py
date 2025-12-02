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
from cuprum.program import Program

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
    "Program",
    "ProgramCatalogue",
    "ProgramEntry",
    "ProjectSettings",
    "UnknownProgramError",
]
