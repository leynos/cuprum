"""Behavioural tests for the program catalogue."""

from __future__ import annotations

import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

import cuprum as c
from cuprum import sh
from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProgramEntry,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from types import ModuleType

    from cuprum.sh import SafeCmd


@scenario("../features/catalogue.feature", "Unknown program is blocked by default")
def test_unknown_program_blocked() -> None:
    """Behavioural guard rail for unknown executables."""


@scenario(
    "../features/catalogue.feature",
    "Projects expose metadata for downstream services",
)
def test_project_metadata_visible() -> None:
    """Behavioural contract for exposing catalogue metadata."""


@scenario(
    "../features/catalogue.feature",
    "Curated program is accepted via the public API",
)
def test_curated_program_accepted() -> None:
    """Behavioural coverage for the public API surface."""


@scenario(
    "../features/catalogue.feature",
    "Safe command builder constructs typed argv",
)
def test_safe_command_builder() -> None:
    """Behavioural coverage for the sh.make safe command factory."""


@given("the default catalogue", target_fixture="catalogue")
def given_default_catalogue() -> ProgramCatalogue:
    """Provide the default catalogue fixture."""
    return DEFAULT_CATALOGUE


@given("the cuprum public API surface", target_fixture="public_api")
def given_public_api() -> ModuleType:
    """Expose the top-level cuprum re-exports to scenarios."""
    return c


@when(
    parsers.parse('I request the program "{program_name}"'),
    target_fixture="catalogue_result",
)
def when_request_program(
    catalogue: ProgramCatalogue,
    program_name: str,
) -> dict[str, object]:
    """Attempt to resolve a program name using the catalogue."""
    result: dict[str, object] = {}
    try:
        result["entry"] = catalogue.lookup(program_name)
    except UnknownProgramError as exc:  # pragma: no cover - behaviour assertion step
        result["error"] = exc
    return result


@when(
    parsers.parse('I look up the curated program "{program_name}"'),
    target_fixture="public_lookup",
)
def when_lookup_curated_program(
    public_api: ModuleType,
    program_name: str,
) -> dict[str, ProgramEntry | UnknownProgramError]:
    """Lookup a curated program through the public API."""
    result: dict[str, ProgramEntry | UnknownProgramError] = {}
    try:
        program = public_api.Program(program_name)
        result["entry"] = public_api.DEFAULT_CATALOGUE.lookup(program)
    except UnknownProgramError as exc:  # pragma: no cover - behaviour assertion step
        result["error"] = exc
    return result


@given(
    parsers.parse('the curated program "{program_name}" is present in the catalogue'),
    target_fixture="curated_program",
)
def given_curated_program(program_name: str) -> Program:
    """Provide a curated Program value for sh.make scenarios."""
    return Program(program_name)


@then("the catalogue rejects it with an unknown program error")
def then_catalogue_rejects(catalogue_result: dict[str, object]) -> None:
    """Assert that an unknown program was rejected."""
    assert "error" in catalogue_result, "Expected an UnknownProgramError"
    assert isinstance(
        catalogue_result["error"],
        UnknownProgramError,
    ), "Error must be an UnknownProgramError instance"


@when("downstream services request visible settings", target_fixture="visible_settings")
def when_request_visible_settings(
    catalogue: ProgramCatalogue,
) -> typ.Mapping[str, ProjectSettings]:
    """Expose project metadata to the scenario."""
    return catalogue.visible_settings()


@when(
    parsers.parse('I build a safe command with "{first}" and "{second}"'),
    target_fixture="safe_command",
)
def when_build_safe_command(
    curated_program: Program,
    first: str,
    second: str,
) -> SafeCmd[str]:
    """Construct a safe command using the sh.make facade."""
    builder = sh.make(curated_program)
    return builder(first, second)


@then(parsers.parse('project "{project_name}" advertises noise rules and docs'))
def then_project_metadata_present(
    visible_settings: typ.Mapping[str, ProjectSettings],
    project_name: str,
) -> None:
    """Ensure downstream services can see project metadata."""
    assert project_name in visible_settings, "Project must be present in settings"
    project = visible_settings[project_name]
    assert project.noise_rules, "Noise rules should be visible to callers"
    assert project.documentation_locations, "Docs should be visible to callers"


@then(
    parsers.parse(
        'the lookup succeeds for project "{project_name}" with a typed program',
    ),
)
def then_lookup_succeeds(
    public_lookup: dict[str, object],
    project_name: str,
) -> None:
    """Validate successful lookups via the public API."""
    assert "entry" in public_lookup, "Expected a successful lookup entry"
    entry = typ.cast("ProgramEntry", public_lookup["entry"])
    assert entry.project_name == project_name, "Project name must match expectation"
    assert isinstance(
        entry.program,
        str,
    ), "Program should behave as str at runtime"
    assert entry.program == c.Program("echo"), (
        "Entry must carry the curated Program value"
    )


@then(parsers.parse('the allowlist accepts the string name "{program_name}"'))
def then_allowlist_accepts_string_name(
    public_api: ModuleType,
    program_name: str,
) -> None:
    """Confirm allowlist permits string inputs for curated programs."""
    assert public_api.DEFAULT_CATALOGUE.is_allowed(program_name), (
        "Allowlist must accept the curated program's string name"
    )


@then("the safe command argv includes the program name and arguments")
def then_safe_command_includes_program(
    safe_command: SafeCmd[str],
) -> None:
    """Verify sh.make prepends the program name and stringifies args."""
    assert safe_command.argv_with_program == (
        safe_command.program,
        "-n",
        "hello world",
    ), "Full argv should include program name and provided arguments"


@then("the safe command exposes project metadata for downstream services")
def then_safe_command_exposes_metadata(
    safe_command: SafeCmd[str],
) -> None:
    """Ensure downstream services can see project metadata via the command."""
    project = safe_command.project
    assert project.noise_rules, "Noise rules should be exposed to consumers"
    assert project.documentation_locations, (
        "Documentation links should be exposed to consumers"
    )
