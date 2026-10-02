"""Unit tests for the sh.make typed command core."""

from __future__ import annotations

import dataclasses as dc
import inspect
import typing as typ

import pytest

from cuprum import ECHO, ExecutionContext, sh
from cuprum.catalogue import (
    ProgramCatalogue,
    ProjectSettings,
    UnknownProgramError,
)
from cuprum.program import Program
from cuprum.sh import SafeCmd, build_argv
from cuprum.sh.factory import _CONTEXT_OPTIONS, _RESERVED_OPTIONS, _RUN_OPTIONS

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


def test_reserved_options_cover_execution_context_and_run_parameters() -> None:
    """The reserved set is exactly the context fields and run parameters.

    The context half is derived from the dataclass; the run half is written by
    hand. Comparing both against the live ``ExecutionContext`` and
    ``SafeCmd.run_sync`` signatures fails when either drifts, so a new
    execution option cannot silently start rendering as a child flag.
    """
    context_fields = {field.name for field in dc.fields(ExecutionContext)}
    run_parameters = {
        name
        for name, parameter in inspect.signature(SafeCmd.run_sync).parameters.items()
        if name != "self" and parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }

    assert context_fields == _CONTEXT_OPTIONS, (
        "the derived context options must match ExecutionContext's fields"
    )
    assert run_parameters == _RUN_OPTIONS, (
        "the handwritten run options must match SafeCmd.run_sync's keyword-only "
        "parameters"
    )
    assert context_fields | run_parameters == _RESERVED_OPTIONS, (
        "the builder must reserve the union of both execution-option sources"
    )


@pytest.mark.parametrize(
    "name",
    sorted(_CONTEXT_OPTIONS - _RUN_OPTIONS),
    ids=lambda name: name,
)
def test_make_rejects_context_field_keywords(name: str) -> None:
    """Context field names are rejected with the ExecutionContext spelling."""
    builder = sh.make(ECHO)

    with pytest.raises(TypeError) as excinfo:
        builder("hello", **{name: "value"})

    assert str(excinfo.value) == (
        f"{name} is an execution option; pass ExecutionContext({name}=...) to run_sync"
    ), "the error must name the correct run_sync spelling"


@pytest.mark.parametrize("name", sorted(_RUN_OPTIONS), ids=lambda name: name)
def test_make_rejects_run_parameter_keywords(name: str) -> None:
    """run_sync parameter names are rejected with the direct spelling."""
    builder = sh.make(ECHO)

    with pytest.raises(TypeError) as excinfo:
        builder("hello", **{name: "value"})

    assert str(excinfo.value) == (
        f"{name} is an execution option; pass {name}=... to run_sync"
    ), "the error must name the run_sync parameter directly"


def test_make_rejects_the_first_reserved_keyword_in_insertion_order(
    tmp_path: Path,
) -> None:
    """The reported keyword is the first reserved one the caller supplied.

    ``timeout`` belongs to both sources; the run-parameter spelling wins so the
    message matches the parameter the caller can pass straight to ``run_sync``.
    """
    builder = sh.make(ECHO)

    with pytest.raises(TypeError) as excinfo:
        builder("x", cwd=tmp_path, env={"A": "1"}, timeout=5)

    assert str(excinfo.value) == (
        "cwd is an execution option; pass ExecutionContext(cwd=...) to run_sync"
    ), "the first reserved keyword in insertion order must be reported"


def test_make_allows_reserved_names_as_positional_arguments(tmp_path: Path) -> None:
    """A tool with a real --cwd flag still receives it positionally."""
    builder = sh.make(ECHO)

    cmd = builder(f"--cwd={tmp_path}", "--timeout=5")

    assert cmd.argv == (
        f"--cwd={tmp_path}",
        "--timeout=5",
    ), "reserved names must remain usable as positional argv elements"
    assert cmd.argv == build_argv(f"--cwd={tmp_path}", "--timeout=5"), (
        "the positional escape hatch must agree with build_argv"
    )


def test_make_still_serializes_non_reserved_keywords(tmp_path: Path) -> None:
    """Names merely resembling the reserved ones keep rendering as flags."""
    builder = sh.make(ECHO)

    cmd = builder(working_dir=tmp_path, stdin_file="payload")

    assert cmd.argv == (
        f"--working-dir={tmp_path}",
        "--stdin-file=payload",
    ), "non-reserved keywords must keep the documented --flag=value rendering"
