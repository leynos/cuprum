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


def cat_program() -> Program:
    """Return the cat program for stream fidelity tests."""
    return Program("cat")


def combine_programs_into_catalogue(
    *programs: Program,
    project_name: str,
    documentation_locations: tuple[str, ...] = (),
) -> ProgramCatalogue:
    """Build a ProgramCatalogue combining multiple programs into one project.

    Parameters
    ----------
    *programs
        Programs to include in the catalogue's allowlist.
    project_name
        Name for the combined project.
    documentation_locations
        Documentation references for the project (default empty).

    Returns
    -------
    ProgramCatalogue
        Catalogue containing all programs under a single project.

    """
    project = ProjectSettings(
        name=project_name,
        programs=programs,
        documentation_locations=documentation_locations,
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,))
