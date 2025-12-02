"""Curated catalogue of allowed executables and their metadata.

Example:
>>> from cuprum.catalogue import DEFAULT_CATALOGUE, ECHO
>>> entry = DEFAULT_CATALOGUE.lookup(ECHO)
>>> (entry.project_name, entry.program)
('core-ops', 'echo')

"""

from __future__ import annotations

import dataclasses as dc
import typing as typ
from types import MappingProxyType

from cuprum.program import Program


def _coerce_program(raw: Program | str) -> Program:
    """Return input as Program for type narrowing; no transformation performed."""
    return Program(raw)


class UnknownProgramError(LookupError):
    """Raised when a program is not present in the catalogue allowlist."""


@dc.dataclass(frozen=True, slots=True)
class ProjectSettings:
    """Metadata shared by a project's curated programs."""

    name: str
    programs: tuple[Program, ...]
    documentation_locations: tuple[str, ...]
    noise_rules: tuple[str, ...]

    def owns(self, program: Program) -> bool:
        """Return True when the program belongs to this project."""
        return program in self.programs


@dc.dataclass(frozen=True, slots=True)
class ProgramEntry:
    """A resolved program with its owning project metadata."""

    program: Program
    project: ProjectSettings

    @property
    def project_name(self) -> str:
        """Return the owning project's name."""
        return self.project.name


class ProgramCatalogue:
    """Catalogue of curated programs with a default allowlist."""

    def __init__(self, *, projects: typ.Iterable[ProjectSettings]) -> None:
        """Build a catalogue from the supplied project definitions."""
        self._projects = self._index_projects(projects)
        self._program_to_project = self._index_programs(self._projects)
        self._allowlist = frozenset(self._program_to_project)
        self._visible_settings_cache = MappingProxyType(self._projects)

    @property
    def allowlist(self) -> frozenset[Program]:
        """Return the curated allowlist of programs."""
        return self._allowlist

    def is_allowed(self, program: Program | str) -> bool:
        """Return True when the program is part of the default allowlist."""
        program_value = _coerce_program(program)
        return program_value in self._allowlist

    def lookup(self, program: Program | str) -> ProgramEntry:
        """Resolve a program into its entry, blocking unknown executables."""
        program_value = _coerce_program(program)
        project = self._program_to_project.get(program_value)
        if project is None:
            msg = f"Program '{program_value}' is not in the catalogue allowlist"
            raise UnknownProgramError(msg)
        return ProgramEntry(program=program_value, project=project)

    def project_for(self, program: Program | str) -> ProjectSettings:
        """Return the owning project for the given program."""
        return self.lookup(program).project

    def visible_settings(self) -> typ.Mapping[str, ProjectSettings]:
        """Expose project metadata to downstream services."""
        return self._visible_settings_cache

    @staticmethod
    def _index_projects(
        projects: typ.Iterable[ProjectSettings],
    ) -> dict[str, ProjectSettings]:
        """Index project settings by name and guard against duplicates."""
        indexed: dict[str, ProjectSettings] = {}
        for project in projects:
            if project.name in indexed:
                msg = f"Project '{project.name}' registered more than once"
                raise ValueError(msg)
            indexed[project.name] = project
        return indexed

    @staticmethod
    def _index_programs(
        projects: dict[str, ProjectSettings],
    ) -> dict[Program, ProjectSettings]:
        """Index programs by value, enforcing unique ownership."""
        program_map: dict[Program, ProjectSettings] = {}
        for project in projects.values():
            for program in project.programs:
                if program in program_map:
                    owner = program_map[program].name
                    msg = f"Program '{program}' already owned by '{owner}'"
                    raise ValueError(msg)
                program_map[program] = project
        return program_map


CORE_OPS_PROJECT = "core-ops"
DOCUMENTATION_PROJECT = "docs"

ECHO = Program("echo")
LS = Program("ls")
DOC_TOOL = Program("mdbook")

DEFAULT_PROJECTS: tuple[ProjectSettings, ...] = (
    ProjectSettings(
        name=CORE_OPS_PROJECT,
        programs=(ECHO, LS),
        documentation_locations=("docs/users-guide.md#program-catalogue",),
        noise_rules=(r"^progress:", r"^note:"),
    ),
    ProjectSettings(
        name=DOCUMENTATION_PROJECT,
        programs=(DOC_TOOL,),
        documentation_locations=("https://docs.example.invalid/cuprum/catalogue",),
        noise_rules=(r"^\[INFO\]",),
    ),
)

DEFAULT_CATALOGUE = ProgramCatalogue(projects=DEFAULT_PROJECTS)

__all__ = [
    "CORE_OPS_PROJECT",
    "DEFAULT_CATALOGUE",
    "DEFAULT_PROJECTS",
    "DOCUMENTATION_PROJECT",
    "DOC_TOOL",
    "ECHO",
    "LS",
    "ProgramCatalogue",
    "ProgramEntry",
    "ProjectSettings",
    "UnknownProgramError",
]
