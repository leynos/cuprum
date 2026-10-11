"""Behavioural tests for scope-aware catalogue resolution.

``test_catalogue_behaviour`` declares the catalogue's lookup and builder
scenarios. The scope-aware resolution scenario lives here instead, with the
steps it alone uses, so neither module has to carry the other's fixtures: the
four steps below are reached by no other scenario in
``tests/features/catalogue.feature``.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, parsers, scenario, then, when

from cuprum import current_context, scoped, sh
from cuprum.catalogue import DEFAULT_CATALOGUE, ProgramCatalogue, UnknownProgramError
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


@scenario(
    "../features/catalogue.feature",
    "A catalogue scope selects the builder catalogue",
)
def test_scoped_catalogue_selects_builder() -> None:
    """Behavioural coverage for scope-aware catalogue resolution."""


@given(
    parsers.parse('a catalogue owning the program "{program_name}"'),
    target_fixture="scoped_catalogue",
)
def given_catalogue_owning(program_name: str) -> ProgramCatalogue:
    """Provide a single-project catalogue for the scoped-resolution scenario.

    Parameters
    ----------
    program_name : str
        The program the catalogue should own.

    Returns
    -------
    ProgramCatalogue
        A catalogue whose sole project owns ``program_name``.
    """
    return ProgramCatalogue.from_programs(program_name)


@when(
    parsers.parse(
        'I build a safe command for "{program_name}" inside that catalogue scope'
    ),
    target_fixture="scoped_builder_result",
)
def when_build_inside_catalogue_scope(
    scoped_catalogue: ProgramCatalogue,
    program_name: str,
) -> dict[str, object]:
    """Build a command inside a catalogue scope, naming no catalogue.

    Parameters
    ----------
    scoped_catalogue : ProgramCatalogue
        The catalogue the scope activates.
    program_name : str
        The program captured from the scenario step text.

    Returns
    -------
    dict[str, object]
        A mapping carrying the built command and the catalogue that produced it.
    """
    result: dict[str, object] = {}
    with scoped(catalogue=scoped_catalogue):
        result["command"] = sh.make(Program(program_name))("--version")
        result["catalogue"] = current_context().catalogue
    return result


@then("the safe command resolves through the scoped catalogue")
def then_command_uses_scoped_catalogue(
    scoped_builder_result: dict[str, object],
    scoped_catalogue: ProgramCatalogue,
) -> None:
    """Confirm the builder resolved its metadata from the scoped catalogue."""
    assert scoped_builder_result["catalogue"] is scoped_catalogue, (
        "The scope should expose the catalogue it activated"
    )
    command = typ.cast("SafeCmd", scoped_builder_result["command"])
    assert command.project is scoped_catalogue.lookup(command.program).project, (
        "The builder should attach metadata from the scoped catalogue"
    )


@then(parsers.parse('the default catalogue still rejects "{program_name}"'))
def then_default_catalogue_rejects(
    program_name: str,
    scoped_catalogue: ProgramCatalogue,
) -> None:
    """Confirm the scoped catalogue is a distinct, narrower one."""
    assert not DEFAULT_CATALOGUE.is_allowed(program_name), (
        "The default catalogue should not allow the scoped program"
    )
    with pytest.raises(UnknownProgramError):
        sh.make(Program(program_name))
