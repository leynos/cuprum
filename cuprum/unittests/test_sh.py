"""Unit tests for the sh.make typed command core."""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import ECHO, sh
from cuprum.catalogue import (
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.sh import SafeCmdBuilder


def test_make_rejects_unknown_program() -> None:
    """Unknown programs are blocked when constructing builders."""
    with pytest.raises(UnknownProgramError):
        sh.make(Program("missing"))


def test_make_returns_callable_and_safe_command_metadata() -> None:
    """sh.make returns SafeCmd instances populated with catalogue metadata."""
    builder = sh.make(ECHO)

    cmd = builder("-n", "hello")

    assert isinstance(cmd, sh.SafeCmd), "Builder should yield SafeCmd instances"
    assert cmd.program == ECHO, "Program should be preserved"
    assert cmd.argv == ("-n", "hello"), "Positional args should be captured"
    assert cmd.argv_with_program == (
        str(ECHO),
        "-n",
        "hello",
    ), "Program name must prefix argv"
    assert cmd.project.name == "core-ops", "Project metadata should be attached"
    assert cmd.project.noise_rules, "Noise rules should be surfaced"
    assert cmd.project.documentation_locations, "Documentation links should surface"


def test_keyword_arguments_are_serialized_to_flags() -> None:
    """Keyword arguments are converted into CLI-style flags."""
    builder = sh.make(ECHO)

    cmd = builder("hello", punctuation="!")

    assert cmd.argv[-2:] == (
        "hello",
        "--punctuation=!",
    ), "Keyword args should become --k=v flags"
    assert cmd.argv_with_program[0] == str(ECHO), "Program must remain first element"


def test_keyword_arguments_normalize_underscores() -> None:
    """Kwarg names are normalized from underscores to hyphens."""
    builder = sh.make(ECHO)

    cmd = builder("hello", user_id=42)

    assert cmd.argv[-1] == "--user-id=42", "Underscores should become hyphens"


def test_arguments_are_stringified_safely(tmp_path: Path) -> None:
    """Non-string arguments are stringified to maintain typed argv."""
    builder = sh.make(ECHO)
    working_dir = tmp_path / "example"

    cmd = builder(working_dir, count=3)

    assert cmd.argv == (
        working_dir.as_posix(),
        "--count=3",
    ), "Arguments must be stringified in order"


@pytest.mark.parametrize(
    "invoke",
    [
        lambda builder: builder(None),
        lambda builder: builder(flag=None),
    ],
    ids=["positional-argument", "keyword-argument"],
)
def test_make_rejects_none_argument(
    invoke: cabc.Callable[[SafeCmdBuilder], object],
) -> None:
    """None as a positional or keyword argument is rejected with a TypeError."""
    builder = sh.make(ECHO)

    with pytest.raises(TypeError) as excinfo:
        invoke(builder)

    assert "None" in str(excinfo.value), "None should be explicitly rejected"


def test_make_supports_custom_catalogue() -> None:
    """Injected catalogues drive metadata visible to downstream services."""
    program = Program("tool")
    custom_project = ProjectSettings(
        name="custom",
        programs=(program,),
        documentation_locations=("docs/runbook.md",),
        noise_rules=(r"^skip-me",),
    )
    catalogue = ProgramCatalogue(projects=(custom_project,))

    cmd = sh.make(program, catalogue=catalogue)("run")

    assert cmd.project is custom_project, "Custom catalogue metadata should be used"
    assert cmd.argv_with_program == (
        str(program),
        "run",
    ), "Full argv should include program and args"
