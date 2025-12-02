"""cuprum package."""

from __future__ import annotations

from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    DEFAULT_PROJECTS,
    DOC_TOOL,
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
