"""Unit tests for the curated program catalogue."""

from __future__ import annotations

import pytest

from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    ECHO,
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program


def test_program_newtype_round_trip() -> None:
    """Program behaves like a string while keeping nominal typing."""
    program = Program("echo")
    assert isinstance(program, str)
    assert program == "echo"


def test_default_allowlist_contains_curated_programs() -> None:
    """The default allowlist surfaces curated program constants."""
    assert ECHO in DEFAULT_CATALOGUE.allowlist
    assert CORE_OPS_PROJECT in DEFAULT_CATALOGUE.visible_settings()


def test_unknown_programs_are_blocked_by_default() -> None:
    """Unknown executables are rejected to maintain safety by default."""
    with pytest.raises(UnknownProgramError):
        DEFAULT_CATALOGUE.lookup("unknown-tool")


def test_visible_settings_surface_project_metadata() -> None:
    """Project metadata is available to downstream services."""
    settings = DEFAULT_CATALOGUE.visible_settings()
    project = settings[CORE_OPS_PROJECT]
    assert project.noise_rules
    assert project.documentation_locations
    assert ECHO in project.programs


def test_catalogue_can_be_extended_safely() -> None:
    """A new catalogue accepts extra projects while blocking unknown ones."""
    docs_project = ProjectSettings(
        name="docs",
        programs=(Program("mdbook"),),
        documentation_locations=("https://example.test/docs/commands",),
        noise_rules=(r"^\[INFO\]",),
    )

    catalogue = ProgramCatalogue(projects=(docs_project,))

    resolved = catalogue.lookup("mdbook")
    assert resolved.program == Program("mdbook")
    assert resolved.project.name == "docs"
    assert catalogue.is_allowed(Program("mdbook")) is True

    with pytest.raises(UnknownProgramError):
        catalogue.lookup("nonexistent")
