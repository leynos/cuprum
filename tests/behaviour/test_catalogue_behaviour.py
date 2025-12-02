"""Behavioural tests for the program catalogue."""

from __future__ import annotations

import typing as typ

from pytest_bdd import given, parsers, scenario, then, when

from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)


@scenario("../features/catalogue.feature", "Unknown program is blocked by default")
def test_unknown_program_blocked() -> None:
    """Behavioural guard rail for unknown executables."""


@scenario(
    "../features/catalogue.feature",
    "Projects expose metadata for downstream services",
)
def test_project_metadata_visible() -> None:
    """Behavioural contract for exposing catalogue metadata."""


@given("the default catalogue", target_fixture="catalogue")
def given_default_catalogue() -> ProgramCatalogue:
    """Provide the default catalogue fixture."""
    return DEFAULT_CATALOGUE


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


@then("the catalogue rejects it with an unknown program error")
def then_catalogue_rejects(catalogue_result: dict[str, object]) -> None:
    """Assert that an unknown program was rejected."""
    assert "error" in catalogue_result, "Expected an UnknownProgramError"
    assert isinstance(catalogue_result["error"], UnknownProgramError)


@when("downstream services request visible settings", target_fixture="visible_settings")
def when_request_visible_settings(
    catalogue: ProgramCatalogue,
) -> typ.Mapping[str, ProjectSettings]:
    """Expose project metadata to the scenario."""
    return catalogue.visible_settings()


@then(parsers.parse('project "{project_name}" advertises noise rules and docs'))
def then_project_metadata_present(
    visible_settings: typ.Mapping[str, ProjectSettings],
    project_name: str,
) -> None:
    """Ensure downstream services can see project metadata."""
    assert project_name in visible_settings
    project = visible_settings[project_name]
    assert project.noise_rules, "Noise rules should be visible to callers"
    assert project.documentation_locations, "Docs should be visible to callers"
