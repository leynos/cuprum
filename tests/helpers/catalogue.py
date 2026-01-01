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


def python_catalogue() -> tuple[ProgramCatalogue, Program]:
    """Construct a catalogue and expose the allowlisted Python program."""
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name="runtime-tests",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md#execution-runtime",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,)), python_program


def python_builder() -> typ.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    catalogue, program = python_catalogue()
    return sh.make(program, catalogue=catalogue)


def cat_catalogue() -> tuple[ProgramCatalogue, Program]:
    """Construct a catalogue and expose the allowlisted cat program."""
    cat_program = Program("cat")
    project = ProjectSettings(
        name="stream-fidelity-tests",
        programs=(cat_program,),
        documentation_locations=("docs/users-guide.md#stream-fidelity",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,)), cat_program
