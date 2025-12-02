"""Unit tests for the curated program catalogue."""

from __future__ import annotations

import pytest

from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    DOC_TOOL,
    ECHO,
    LS,
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program


def test_program_newtype_round_trip() -> None:
    """Program behaves like a string while keeping nominal typing."""
    program = Program("echo")
    assert isinstance(program, str), "Program should subtype str for ergonomics"
    assert program == "echo", "Program must preserve wrapped value"


def test_default_allowlist_contains_curated_programs() -> None:
    """The default allowlist surfaces curated program constants."""
    assert ECHO in DEFAULT_CATALOGUE.allowlist, "Echo missing from allowlist"
    assert CORE_OPS_PROJECT in DEFAULT_CATALOGUE.visible_settings(), (
        "Core project metadata not exposed"
    )
    assert DEFAULT_CATALOGUE.is_allowed("ls"), "String program names should pass"


def test_unknown_programs_are_blocked_by_default() -> None:
    """Unknown executables are rejected to maintain safety by default."""
    with pytest.raises(UnknownProgramError):
        DEFAULT_CATALOGUE.lookup("unknown-tool")


def test_visible_settings_surface_project_metadata() -> None:
    """Project metadata is available to downstream services."""
    settings = DEFAULT_CATALOGUE.visible_settings()
    project = settings[CORE_OPS_PROJECT]
    assert project.noise_rules, "Noise rules should be populated"
    assert project.documentation_locations, "Docs links should be populated"
    assert ECHO in project.programs, "Project should enumerate its programs"
    with pytest.raises(TypeError):
        settings[CORE_OPS_PROJECT] = project  # type: ignore[index]


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
    assert resolved.program == Program("mdbook"), "Lookup returns typed program"
    assert resolved.project.name == "docs", "Owning project should be attached"
    assert catalogue.is_allowed(Program("mdbook")) is True, (
        "Allowlist must accept known program"
    )

    with pytest.raises(UnknownProgramError):
        catalogue.lookup("nonexistent")


def test_duplicate_project_names_are_rejected() -> None:
    """Duplicate project names raise an error during catalogue construction."""
    dup = ProjectSettings(
        name="duplicate",
        programs=(Program("tool"),),
        documentation_locations=("https://example.test/docs",),
        noise_rules=(r"^info",),
    )
    with pytest.raises(ValueError, match="duplicate") as exc:
        ProgramCatalogue(projects=(dup, dup))
    assert "duplicate" in str(exc.value), "Error message must mention duplicate name"


def test_duplicate_programs_across_projects_are_rejected() -> None:
    """The same program cannot be owned by two projects."""
    shared = Program("shared")
    first = ProjectSettings(
        name="first",
        programs=(shared,),
        documentation_locations=("https://example.test/first",),
        noise_rules=(r"^first",),
    )
    second = ProjectSettings(
        name="second",
        programs=(shared,),
        documentation_locations=("https://example.test/second",),
        noise_rules=(r"^second",),
    )
    with pytest.raises(ValueError, match="shared") as exc:
        ProgramCatalogue(projects=(first, second))
    assert "shared" in str(exc.value), "Error must mention the shared program name"
    assert "first" in str(exc.value), (
        "Error must include the original owning project name"
    )


def test_coercion_accepts_program_and_string() -> None:
    """Allowlist checks accept both Program and raw strings."""
    assert DEFAULT_CATALOGUE.is_allowed(Program("echo")), (
        "Program instance must be accepted by is_allowed"
    )
    assert DEFAULT_CATALOGUE.is_allowed("echo"), "Raw strings should also be accepted"


def test_program_hash_and_equality_usage() -> None:
    """Program can be used as a dict key without surprising behaviour."""
    key = Program("ls")
    lookup = {key: "ok"}
    assert lookup[Program("ls")] == "ok", "Program keys should hash consistently"
    assert DEFAULT_CATALOGUE.lookup(LS).program == Program("ls"), (
        "Lookup should keep nominal type"
    )
    assert DEFAULT_CATALOGUE.lookup(DOC_TOOL).project.name == "docs", (
        "DOC_TOOL should belong to docs project"
    )
