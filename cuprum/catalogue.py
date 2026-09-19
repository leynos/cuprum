"""Curated catalogue of allowed executables and their metadata.

Example:
>>> from cuprum.catalogue import DEFAULT_CATALOGUE, ECHO
>>> entry = DEFAULT_CATALOGUE.lookup(ECHO)
>>> (entry.project_name, entry.program)
('core-ops', 'echo')

"""

from __future__ import annotations

import collections.abc as cabc
import dataclasses as dc
from types import MappingProxyType

from cuprum._catalogue_defaults import (
    CORE_OPS_PROJECT,
    DEFAULT_PROJECT_DATA,
    DOC_TOOL,
    DOCUMENTATION_PROJECT,
    ECHO,
    GIT,
    LS,
    RSYNC,
    TAR,
)
from cuprum._catalogue_helpers import coerce_program, derive_project_name
from cuprum.program import (
    Program,  # ruff: ignore[typing-only-first-party-import] - public annotations must resolve at runtime,
)


class UnknownProgramError(LookupError):
    """Raised when a program is not present in the catalogue allowlist."""


class DuplicateProjectError(ValueError):
    """Raised when a project name is registered more than once.

    Parameters
    ----------
    project_name : str
        The duplicated project name.

    Attributes
    ----------
    project_name : str
        The duplicated project name.
    """

    def __init__(self, project_name: str) -> None:
        """Record the duplicated project name and build the message."""
        self.project_name = project_name
        super().__init__(f"Project '{project_name}' registered more than once")


class DuplicateProgramError(ValueError):
    """Raised when a program is claimed by more than one project.

    Parameters
    ----------
    program : Program
        The contested program.
    owner : str
        The name of the project that already owns the program.

    Attributes
    ----------
    program : Program
        The contested program.
    owner : str
        The existing owner's project name.
    """

    def __init__(self, program: Program, owner: str) -> None:
        """Record the contested program and its existing owner."""
        self.program = program
        self.owner = owner
        super().__init__(f"Program '{program}' already owned by '{owner}'")


@dc.dataclass(frozen=True, slots=True)
class ProjectSettings:
    """Metadata shared by a project's curated programs.

    ``documentation_locations`` and ``noise_rules`` default to empty tuples so
    a small script can declare a project from its name and programs alone.

    Attributes
    ----------
    name : str
        The project's descriptive name.
    programs : tuple[Program, ...]
        Curated programs owned by the project.
    documentation_locations : tuple[str, ...]
        Runbook/reference links visible through ``visible_settings``. Empty means no
        documentation references are declared.
    noise_rules : tuple[str, ...]
        Patterns a logger may drop; Cuprum stores them but does not apply them.
        Empty means no project output lines are marked as noise.
    """

    name: str
    programs: tuple[Program, ...]
    documentation_locations: tuple[str, ...] = ()
    noise_rules: tuple[str, ...] = ()

    def owns(self, program: Program) -> bool:
        """Return True when the program belongs to this project.

        Parameters
        ----------
        program : Program
            The program to test for membership in this project.

        Returns
        -------
        bool
            True if the program is one of this project's programs.
        """
        return program in self.programs


@dc.dataclass(frozen=True, slots=True)
class ProgramEntry:
    """A resolved program with its owning project metadata."""

    program: Program
    project: ProjectSettings

    @property
    def project_name(self) -> str:
        """The owning project's name.

        Returns
        -------
        str
            The name of the project that registered this program.
        """
        return self.project.name


class _VisibleSettings(cabc.Mapping[str, ProjectSettings]):
    """Cached, read-only catalogue settings with legacy call compatibility."""

    def __init__(self, settings: cabc.Mapping[str, ProjectSettings]) -> None:
        """Store an immutable view of the catalogue settings."""
        self._settings = MappingProxyType(settings)

    def __getitem__(self, key: str) -> ProjectSettings:
        """Return the settings registered for ``key``."""
        return self._settings[key]

    def __iter__(self) -> cabc.Iterator[str]:
        """Iterate over project names."""
        return iter(self._settings)

    def __len__(self) -> int:
        """Return the number of visible projects."""
        return len(self._settings)

    def __call__(self) -> _VisibleSettings:
        """Return this mapping for callers using the former method form."""
        return self


class ProgramCatalogue:
    """Catalogue of curated programs with a default allowlist."""

    def __init__(self, *, projects: cabc.Iterable[ProjectSettings]) -> None:
        """Build a catalogue from the supplied project definitions."""
        self._projects = self._index_projects(projects)
        self._program_to_project = self._index_programs(self._projects)
        self._allowlist = frozenset(self._program_to_project)
        self._visible_settings_cache = _VisibleSettings(self._projects)

    @classmethod
    def from_programs(
        cls,
        *programs: Program | str,
        name: str | None = None,
        documentation_locations: tuple[str, ...] = (),
        noise_rules: tuple[str, ...] = (),
    ) -> ProgramCatalogue:
        """Build a single-project catalogue from the given programs.

        Parameters
        ----------
        *programs : Program | str
            Programs to allowlist; bare names and absolute paths are accepted.
        name : str | None, optional
            Project name, derived from the programs' base names when omitted.
        documentation_locations : tuple[str, ...], optional
            Documentation references for the project.
        noise_rules : tuple[str, ...], optional
            Regular expressions for output lines a logger may drop.

        Returns
        -------
        ProgramCatalogue
            A catalogue whose sole project owns the supplied programs.

        Raises
        ------
        ValueError
            If no programs are supplied.
        DuplicateProgramError
            If a program is supplied more than once.

        Examples
        --------
        >>> from cuprum import ProgramCatalogue
        >>> catalogue = ProgramCatalogue.from_programs("git", "cargo")
        >>> sorted(catalogue.allowlist)
        ['cargo', 'git']
        >>> catalogue.lookup("git").project_name
        'git-cargo'
        """  # ruff: ignore[docstring-extraneous-exception] - DuplicateProgramError propagates from the constructor
        if not programs:
            msg = "from_programs requires at least one program"
            raise ValueError(msg)
        coerced = tuple(coerce_program(program) for program in programs)
        project = ProjectSettings(
            name=derive_project_name(coerced) if name is None else name,
            programs=coerced,
            documentation_locations=documentation_locations,
            noise_rules=noise_rules,
        )
        return cls(projects=(project,))

    @classmethod
    def from_project(cls, settings: ProjectSettings) -> ProgramCatalogue:
        """Build a catalogue containing a supplied project.

        Parameters
        ----------
        settings : ProjectSettings
            The sole project to register.

        Returns
        -------
        ProgramCatalogue
            A catalogue exposing the supplied project's allowlist and metadata.

        Raises
        ------
        DuplicateProgramError
            If ``settings`` declares a program more than once.

        Examples
        --------
        >>> from cuprum import Program, ProgramCatalogue, ProjectSettings
        >>> settings = ProjectSettings(name="tools", programs=(Program("git"),))
        >>> ProgramCatalogue.from_project(settings).is_allowed("git")
        True
        """  # ruff: ignore[docstring-extraneous-exception] - DuplicateProgramError propagates from the constructor
        return cls(projects=(settings,))

    @property
    def allowlist(self) -> frozenset[Program]:
        """The curated allowlist of programs.

        Returns
        -------
        frozenset[Program]
            Every program registered in the catalogue. The returned set cannot
            be modified.
        """
        return self._allowlist

    def is_allowed(self, program: Program | str) -> bool:
        """Return True when the program is part of the default allowlist.

        Parameters
        ----------
        program : Program | str
            The program to test against the curated default allowlist.

        Returns
        -------
        bool
            True if the program is present in the curated allowlist.
        """
        program_value = coerce_program(program)
        return program_value in self._allowlist

    def lookup(self, program: Program | str) -> ProgramEntry:
        """Resolve a program into its entry, blocking unknown executables.

        Parameters
        ----------
        program : Program | str
            The program to resolve into its catalogue entry.

        Returns
        -------
        ProgramEntry
            The resolved entry with its owning project metadata.

        Raises
        ------
        UnknownProgramError
            If the program is not present in the catalogue allowlist.
        """
        program_value = coerce_program(program)
        project = self._program_to_project.get(program_value)
        if project is None:
            msg = f"Program '{program_value}' is not in the catalogue allowlist"
            raise UnknownProgramError(msg)
        return ProgramEntry(program=program_value, project=project)

    def project_for(self, program: Program | str) -> ProjectSettings:
        """Return the owning project for the given program.

        Parameters
        ----------
        program : Program | str
            The program whose owning project is returned.

        Returns
        -------
        ProjectSettings
            The project that owns the given program.

        Raises
        ------
        UnknownProgramError
            If the program is not present in this catalogue.
        """  # ruff: ignore[docstring-extraneous-exception] - UnknownProgramError propagates from lookup
        return self.lookup(program).project

    @property
    def visible_settings(self) -> _VisibleSettings:
        """Cached, read-only settings indexed by project name.

        Returns
        -------
        _VisibleSettings
            The same immutable mapping for the catalogue lifetime. Access it
            as a property; calling it remains a temporary compatibility form.
        """
        return self._visible_settings_cache

    @staticmethod
    def _index_projects(
        projects: cabc.Iterable[ProjectSettings],
    ) -> dict[str, ProjectSettings]:
        """Index project settings by name and guard against duplicates."""
        indexed: dict[str, ProjectSettings] = {}
        for project in projects:
            if project.name in indexed:
                raise DuplicateProjectError(project.name)
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
                    raise DuplicateProgramError(program, program_map[program].name)
                program_map[program] = project
        return program_map


DEFAULT_PROJECTS: tuple[ProjectSettings, ...] = tuple(
    ProjectSettings(
        name=name,
        programs=programs,
        documentation_locations=documentation_locations,
        noise_rules=noise_rules,
    )
    for name, programs, documentation_locations, noise_rules in DEFAULT_PROJECT_DATA
)

DEFAULT_CATALOGUE = ProgramCatalogue(projects=DEFAULT_PROJECTS)

__all__ = [
    "CORE_OPS_PROJECT",
    "DEFAULT_CATALOGUE",
    "DEFAULT_PROJECTS",
    "DOCUMENTATION_PROJECT",
    "DOC_TOOL",
    "ECHO",
    "GIT",
    "LS",
    "RSYNC",
    "TAR",
    "DuplicateProgramError",
    "DuplicateProjectError",
    "ProgramCatalogue",
    "ProgramEntry",
    "ProjectSettings",
    "UnknownProgramError",
]
