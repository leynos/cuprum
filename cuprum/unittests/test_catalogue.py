"""Unit tests for the curated program catalogue."""

from __future__ import annotations

import concurrent.futures
import sys
import threading
import typing as typ

import pytest

from cuprum.catalogue import (
    CORE_OPS_PROJECT,
    DEFAULT_CATALOGUE,
    DOC_TOOL,
    ECHO,
    GIT,
    LS,
    RSYNC,
    TAR,
    DuplicateProgramError,
    DuplicateProjectError,
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
    assert DOC_TOOL in DEFAULT_CATALOGUE.allowlist, "Doc tool missing from allowlist"
    assert GIT in DEFAULT_CATALOGUE.allowlist, "Git missing from allowlist"
    assert RSYNC in DEFAULT_CATALOGUE.allowlist, "Rsync missing from allowlist"
    assert TAR in DEFAULT_CATALOGUE.allowlist, "Tar missing from allowlist"
    assert CORE_OPS_PROJECT in DEFAULT_CATALOGUE.visible_settings, (
        "Core project metadata not exposed"
    )
    assert DEFAULT_CATALOGUE.is_allowed("ls"), "String program names should pass"


def test_unknown_programs_are_blocked_by_default() -> None:
    """Unknown executables are rejected to maintain safety by default."""
    with pytest.raises(UnknownProgramError):
        DEFAULT_CATALOGUE.lookup("unknown-tool")


def test_visible_settings_surface_project_metadata() -> None:
    """Project metadata is available to downstream services."""
    settings = DEFAULT_CATALOGUE.visible_settings
    assert settings is DEFAULT_CATALOGUE.visible_settings, (
        "Visible settings should reuse the catalogue's read-only view"
    )
    assert settings() is settings, (
        "the former visible_settings() call form must return the cached view"
    )
    project = settings[CORE_OPS_PROJECT]
    assert project.noise_rules, "Noise rules should be populated"
    assert project.documentation_locations, "Docs links should be populated"
    assert ECHO in project.programs, "Project should enumerate its programs"
    # Cast away the read-only static type to exercise the runtime guard on the
    # published read-only mapping.
    with pytest.raises(TypeError):
        typ.cast("dict[str, ProjectSettings]", settings)[CORE_OPS_PROJECT] = project


def test_visible_settings_concurrent_first_access_reuses_view() -> None:
    """Concurrent first access should publish one read-only view instance."""
    catalogue = ProgramCatalogue(projects=DEFAULT_CATALOGUE.visible_settings.values())
    barrier = threading.Barrier(8)

    def access_visible_settings(_: int) -> object:
        """Synchronize one worker before reading the catalogue view."""
        barrier.wait()
        return catalogue.visible_settings

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        settings = tuple(executor.map(access_visible_settings, range(8)))

    assert all(view is settings[0] for view in settings), (
        "concurrent first access should return one shared mapping proxy"
    )


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
    with pytest.raises(DuplicateProjectError, match="duplicate") as exc:
        ProgramCatalogue(projects=(dup, dup))
    assert exc.value.project_name == "duplicate", (
        "Error must carry the duplicated project name"
    )


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
    with pytest.raises(DuplicateProgramError, match="shared") as exc:
        ProgramCatalogue(projects=(first, second))
    assert exc.value.program == shared, "Error must carry the contested program"
    assert exc.value.owner == "first", (
        "Error must carry the original owning project name"
    )


def test_coercion_accepts_program_and_string() -> None:
    """Allowlist checks accept both Program and raw strings."""
    assert DEFAULT_CATALOGUE.is_allowed(Program("echo")), (
        "Program instance must be accepted by is_allowed"
    )
    assert DEFAULT_CATALOGUE.is_allowed("echo"), "Raw strings should also be accepted"


def test_project_settings_defaults_to_empty_metadata() -> None:
    """A project may declare only a name and its programs."""
    project = ProjectSettings(name="bare", programs=(Program("tool"),))

    assert project.documentation_locations == (), "Docs links should default to empty"
    assert project.noise_rules == (), "Noise rules should default to empty"


def test_from_programs_builds_single_project_catalogue() -> None:
    """The convenience constructor allowlists every supplied program."""
    catalogue = ProgramCatalogue.from_programs("git", "cargo")

    assert catalogue.allowlist == frozenset({Program("git"), Program("cargo")}), (
        "Allowlist must contain exactly the supplied programs"
    )
    for program in ("git", "cargo"):
        assert catalogue.is_allowed(program), "Coerced string should be allowed"
        assert catalogue.lookup(program).program == Program(program), (
            "Lookup must resolve the supplied program"
        )


def test_from_programs_derives_name_from_program_base_names() -> None:
    """The default project name joins the programs' base names."""
    catalogue = ProgramCatalogue.from_programs("git", "cargo")
    absolute = ProgramCatalogue.from_programs("/usr/bin/git")

    assert catalogue.lookup("git").project_name == "git-cargo", (
        "Default name must join base names with a hyphen"
    )
    assert absolute.lookup("/usr/bin/git").project_name == "git", (
        "Absolute paths must reduce to their base name"
    )


def test_from_programs_derives_windows_base_name_on_any_host() -> None:
    """A drive path reduces to its base name regardless of host platform."""
    windows_path = r"C:\tools\git.exe"
    catalogue = ProgramCatalogue.from_programs(windows_path, "cargo")

    assert catalogue.lookup(windows_path).project_name == "git.exe-cargo", (
        "Windows drive paths must reduce to their base name on every host"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows treats a backslash as a path separator",
)
def test_from_programs_keeps_posix_backslash_in_base_name() -> None:
    """Only Windows drive paths are re-parsed; POSIX names keep a backslash."""
    program = r"weird\name"
    catalogue = ProgramCatalogue.from_programs(program)

    assert catalogue.lookup(program).project_name == program, (
        "A POSIX filename containing a backslash must not be split on it"
    )


def test_from_programs_accepts_explicit_name_and_metadata() -> None:
    """A caller-supplied name overrides the derived default."""
    catalogue = ProgramCatalogue.from_programs(
        Program("git"),
        name="repo-tools",
        documentation_locations=("docs/scripting-standards.md",),
        noise_rules=(r"^hint:",),
    )

    project = catalogue.visible_settings["repo-tools"]
    assert project.documentation_locations == ("docs/scripting-standards.md",), (
        "Docs links must be carried into the project"
    )
    assert project.noise_rules == (r"^hint:",), "Noise rules must be carried over"


def test_from_programs_requires_at_least_one_program() -> None:
    """An empty call is a usage error rather than an empty catalogue."""
    with pytest.raises(ValueError, match="at least one program"):
        ProgramCatalogue.from_programs()


def test_from_programs_rejects_duplicate_programs() -> None:
    """Repeated programs fail exactly as the full constructor does."""
    with pytest.raises(DuplicateProgramError, match="git") as exc:
        ProgramCatalogue.from_programs("git", "git")
    assert exc.value.program == Program("git"), "Error must carry the contested program"
    assert exc.value.owner == "git-git", (
        "Error must name the project that registered the program first"
    )


def test_from_project_preserves_allowlist_and_metadata() -> None:
    """A prepared project becomes the catalogue's sole visible project."""
    settings = ProjectSettings(
        name="gate-runner",
        programs=(Program("cargo"),),
        documentation_locations=("docs/runbooks/rust-tests.md",),
        noise_rules=(r"^warning:",),
    )

    catalogue = ProgramCatalogue.from_project(settings=settings)

    assert catalogue.allowlist == frozenset({Program("cargo")}), (
        "Allowlist must contain the supplied project's program"
    )
    assert catalogue.visible_settings == {"gate-runner": settings}, (
        "Visible settings must preserve the supplied project metadata"
    )


def test_from_project_rejects_repeated_programs() -> None:
    """Repeated programs fail through the normal catalogue constructor path."""
    settings = ProjectSettings(
        name="duplicate-tools",
        programs=(Program("cargo"), Program("cargo")),
    )

    with pytest.raises(DuplicateProgramError, match="cargo"):
        ProgramCatalogue.from_project(settings=settings)


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
