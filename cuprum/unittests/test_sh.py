"""Unit tests for the sh.make typed command core."""

from __future__ import annotations

import dataclasses as dc
import inspect
import typing as typ

import pytest

from cuprum import (
    ECHO,
    ExecutionContext,
    ForbiddenProgramError,
    ScopeConfig,
    scoped,
    sh,
)
from cuprum.catalogue import (
    DEFAULT_CATALOGUE,
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


@pytest.mark.parametrize(
    ("invoke", "offending_value"),
    [
        pytest.param(lambda builder: builder(b"release"), b"release", id="release"),
        pytest.param(lambda builder: builder(b"v\xff"), b"v\xff", id="non-utf8"),
        pytest.param(
            lambda builder: builder(tag=b"release"),
            b"release",
            id="keyword-value",
        ),
    ],
)
def test_make_rejects_bytes_argument(
    invoke: cabc.Callable[[SafeCmdBuilder], object],
    offending_value: bytes,
) -> None:
    """Bytes values in positional and keyword arguments raise TypeError."""
    builder = sh.make(ECHO)

    with pytest.raises(TypeError, match="bytes is not a valid argv element") as excinfo:
        invoke(builder)

    assert repr(offending_value) in str(excinfo.value), (
        "The error should identify the offending bytes value"
    )


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


# =============================================================================
# Scoped catalogue resolution
# =============================================================================


def test_make_uses_the_scoped_catalogue_without_repeating_it() -> None:
    """A catalogue scope supplies the catalogue when the call omits one."""
    gh = Program("gh")
    project = ProjectSettings(name="gh-project", programs=(gh,))
    catalogue = ProgramCatalogue(projects=(project,))

    with scoped(catalogue=catalogue):
        cmd = sh.make(gh)("--version")

    assert cmd.program == gh, "Scoped catalogue should resolve the program"
    assert cmd.project is project, "Scoped catalogue metadata should be attached"
    assert cmd.argv_with_program == (str(gh), "--version"), (
        "Scoped resolution should not alter argv construction"
    )


def test_make_rejects_programs_outside_the_scoped_catalogue() -> None:
    """Membership is checked against the scoped catalogue at construction."""
    catalogue = ProgramCatalogue.from_programs(Program("gh"))

    with scoped(catalogue=catalogue), pytest.raises(UnknownProgramError, match=ECHO):
        sh.make(ECHO)


def test_make_falls_back_to_default_catalogue_without_a_scope() -> None:
    """Outside any catalogue scope the default catalogue still applies."""
    project = sh.make(ECHO)("-n", "hello").project

    assert project is DEFAULT_CATALOGUE.lookup(ECHO).project, (
        "An unscoped builder should resolve against the default catalogue"
    )


def test_make_inherits_the_catalogue_through_an_allowlist_only_scope() -> None:
    """A scope that names no catalogue leaves the active catalogue in place."""
    gh = Program("gh")
    catalogue = ProgramCatalogue.from_programs(gh)

    with (
        scoped(catalogue=catalogue),
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
    ):
        cmd = sh.make(gh)("--version")

    assert cmd.project is catalogue.lookup(gh).project, (
        "An allowlist-only scope should inherit the active catalogue"
    )


def test_make_prefers_an_explicit_catalogue_over_the_scoped_one() -> None:
    """An explicit catalogue argument outranks the active scope."""
    gh = Program("gh")
    scoped_catalogue = ProgramCatalogue.from_programs(ECHO)
    explicit_catalogue = ProgramCatalogue.from_programs(gh)

    with scoped(catalogue=scoped_catalogue):
        cmd = sh.make(gh, catalogue=explicit_catalogue)("--version")

    assert cmd.project is explicit_catalogue.lookup(gh).project, (
        "The explicit catalogue argument should win over the active scope"
    )


def test_builder_from_a_foreign_catalogue_fails_only_at_run_time() -> None:
    """Construction checks the catalogue; the scope checks the allowlist."""
    scoped_catalogue = ProgramCatalogue.from_programs(ECHO)
    foreign_catalogue = ProgramCatalogue.from_programs(Program("cat"))

    with scoped(catalogue=scoped_catalogue):
        # Construction consults the explicit catalogue, so it succeeds even
        # though the enclosing scope does not permit the program.
        cmd = sh.make(Program("cat"), catalogue=foreign_catalogue)("--version")
        with pytest.raises(ForbiddenProgramError, match="cat"):
            cmd.run_sync()


def test_make_uses_the_innermost_scoped_catalogue() -> None:
    """Nested scopes resolve against the innermost catalogue in force."""
    outer_program = Program("outer-tool")
    inner_program = Program("inner-tool")
    outer_catalogue = ProgramCatalogue.from_programs(outer_program)
    inner_catalogue = ProgramCatalogue.from_programs(inner_program)

    with scoped(catalogue=outer_catalogue):
        before = sh.make(outer_program)("run")
        with scoped(catalogue=inner_catalogue):
            inner = sh.make(inner_program)("run")
        after = sh.make(outer_program)("run")

    assert before.project is outer_catalogue.lookup(outer_program).project, (
        "The outer catalogue should resolve the builder before the inner scope opens"
    )
    assert inner.project is inner_catalogue.lookup(inner_program).project, (
        "The innermost catalogue should resolve the builder"
    )
    assert after.project is outer_catalogue.lookup(outer_program).project, (
        "Exiting the inner scope should restore the outer catalogue"
    )


def test_builder_keeps_its_catalogue_after_the_scope_exits() -> None:
    """Resolution happens once, so a builder outlives the scope that made it."""
    gh = Program("gh")
    catalogue = ProgramCatalogue.from_programs(gh)

    with scoped(catalogue=catalogue):
        builder = sh.make(gh)

    assert builder("--version").project is catalogue.lookup(gh).project, (
        "A builder should keep the catalogue resolved at construction"
    )
