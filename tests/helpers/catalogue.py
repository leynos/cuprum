"""Shared helpers for building test catalogues and builders."""

from __future__ import annotations

import sys
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


def python_catalogue() -> ProgramCatalogue:
    """Construct a catalogue that allowlists the current Python executable."""
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name="runtime-tests",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md#execution-runtime",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,))


def python_builder() -> typ.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    catalogue = python_catalogue()
    program = next(iter(catalogue.allowlist))
    return sh.make(program, catalogue=catalogue)
