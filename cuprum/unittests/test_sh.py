"""Unit tests for the sh.make typed command core."""

from __future__ import annotations

import os
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

    from cuprum.sh import ArgValue, SafeCmdBuilder


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


class _FakePath(os.PathLike[str]):
    """A ``os.PathLike[str]`` that is deliberately not a ``pathlib.Path``."""

    def __init__(self, path: str) -> None:
        """Wrap ``path`` behind the ``os.PathLike`` protocol."""
        self._path = path

    def __fspath__(self) -> str:
        """Return the wrapped path string."""
        return self._path


_UNSUPPORTED_VALUES = [
    pytest.param(object(), id="object"),
    pytest.param(b"raw", id="bytes"),
    pytest.param(["a", "b"], id="list"),
    pytest.param(_FakePath("example/path"), id="custom-path-like"),
]


@pytest.mark.parametrize("value", _UNSUPPORTED_VALUES)
@pytest.mark.parametrize(
    "invoke",
    [
        pytest.param(lambda builder, value: builder(value), id="positional"),
        pytest.param(lambda builder, value: builder(flag=value), id="keyword"),
    ],
)
def test_unsupported_argument_types_are_rejected(
    invoke: cabc.Callable[[SafeCmdBuilder, ArgValue], object],
    value: object,
) -> None:
    """Unsupported types raise TypeError instead of being stringified."""
    builder = sh.make(ECHO)
    # Deliberately defeat static typing: the property under test is the
    # runtime rejection of the value, which the annotations forbid.
    poisoned = typ.cast("ArgValue", value)

    with pytest.raises(TypeError) as excinfo:
        invoke(builder, poisoned)

    assert type(value).__name__ in str(excinfo.value), (
        "The error must name the offending type"
    )
    assert "sh.make" in str(excinfo.value), "The error must name sh.make"


def test_none_argument_error_message_is_exact() -> None:
    """``None`` keeps its dedicated message rather than the generic one."""
    builder = sh.make(ECHO)
    # Deliberately defeat static typing: the contract under test is the
    # runtime rejection of None, which the annotations forbid.
    poisoned = typ.cast("ArgValue", None)

    with pytest.raises(TypeError) as excinfo:
        builder(poisoned)

    assert str(excinfo.value) == "None is not a valid argv element for sh.make", (
        "The historical None message must not drift"
    )


def test_path_arguments_serialize_to_their_as_posix_form(tmp_path: Path) -> None:
    """``Path`` values serialize through ``str()`` in both positions."""
    builder = sh.make(ECHO)
    target = tmp_path / "nested" / "file.txt"

    cmd = builder(target, destination=target)

    assert cmd.argv == (
        target.as_posix(),
        f"--destination={target.as_posix()}",
    ), "Path values must serialize to their string form"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(True, "--porcelain=True", id="true"),
        pytest.param(False, "--porcelain=False", id="false"),
    ],
)
def test_keyword_booleans_serialize_as_values_not_switches(
    value: bool,
    expected: str,
) -> None:
    """Booleans become ``--flag=<bool>`` rather than presence switches."""
    builder = sh.make(ECHO)

    cmd = builder(porcelain=value)

    assert cmd.argv == (expected,), "A boolean flag must carry its value explicitly"


def test_mixed_call_preserves_positional_then_keyword_order() -> None:
    """Positionals keep their order ahead of generated keyword flags."""
    builder = sh.make(ECHO)

    cmd = builder("first", 2, 3.5, porcelain=True, destination="out")

    assert cmd.argv == (
        "first",
        "2",
        "3.5",
        "--porcelain=True",
        "--destination=out",
    ), "Mixed calls must serialize positionals first, then flags in order"


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
