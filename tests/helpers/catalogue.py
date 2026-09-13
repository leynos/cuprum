"""Shared helpers for building test catalogues and builders."""

from __future__ import annotations

import dataclasses as dc
import sys
import typing as typ
from pathlib import Path

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


@dc.dataclass(frozen=True, slots=True)
class PythonCatalogue:
    """A catalogue paired with its allowlisted program and command builder.

    Bundling the three together lets a single pytest fixture hand tests
    everything they need to build commands without re-deriving the catalogue:
    ``program`` is the allowlist entry to pass to ``scoped``, and ``builder``
    already binds ``program`` to ``catalogue``.
    """

    catalogue: ProgramCatalogue
    program: Program
    builder: cabc.Callable[..., SafeCmd]


def python_catalogue() -> tuple[ProgramCatalogue, Program]:
    """Construct a catalogue and expose the allowlisted Python program.

    Returns
    -------
    tuple[ProgramCatalogue, Program]
        The catalogue and the Python program it allowlists.
    """
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name="runtime-tests",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md#execution-runtime",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,)), python_program


def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    cabc.Callable[..., SafeCmd]
        A builder that produces SafeCmd instances for the interpreter.
    """
    return build_python_catalogue_env().builder


def build_python_catalogue_env() -> PythonCatalogue:
    """Build the interpreter catalogue, its allowlisted program, and a builder.

    Returns
    -------
    PythonCatalogue
        The catalogue, the Python program it allowlists, and a builder bound
        to both. The catalogue is constructed once so the builder and the
        allowlist entry refer to the same instance.
    """
    catalogue, program = python_catalogue()
    return PythonCatalogue(
        catalogue=catalogue,
        program=program,
        builder=sh.make(program, catalogue=catalogue),
    )


def cat_program() -> Program:
    """Return the cat program for stream fidelity tests.

    Returns
    -------
    Program
        The ``cat`` program.
    """
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
