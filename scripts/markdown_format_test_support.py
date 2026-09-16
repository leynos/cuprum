"""Support process-boundary tests for the Markdown Make targets."""

from __future__ import annotations

import dataclasses as dc
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - controlled command boundaries are under test.
import sys
import textwrap
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MARKDOWN_FILES = (
    Path("guide.md"),
    Path("guide.markdown"),
    Path("guide.mdx"),
    Path("nested") / "space name.md",
    Path("nested") / "line\nbreak.md",
    Path("-leading-option.md"),
)


@dc.dataclass(frozen=True, slots=True)
class MarkdownFormatterTools:
    """Keep formatter doubles and their shared transcript together."""

    formatter: Path
    linter: Path
    call_log: Path


class MarkdownFormatterCall(typ.TypedDict, total=False):
    """Describe one validated formatter-double invocation."""

    tool: str
    paths: list[str]
    args: list[str]


@dc.dataclass(frozen=True, slots=True)
class MarkdownTargetOptions:
    """Keep optional Make-target test inputs together."""

    discovery: str | None = None
    environment: cabc.Mapping[str, str] | None = None


DEFAULT_TARGET_OPTIONS = MarkdownTargetOptions()


class UnavailableTestDependencyError(RuntimeError):
    """Report a required process-boundary test dependency that is unavailable."""


class FormatterCallSchemaError(TypeError):
    """Report a formatter-double transcript entry that violates its schema."""

    def __init__(self, field: str) -> None:
        """Name the invalid formatter-call field."""
        super().__init__(f"formatter call has an invalid {field} field")


def run_process(
    command: list[str],
    environment: cabc.Mapping[str, str],
    current_directory: Path,
) -> subprocess.CompletedProcess[str]:
    """Run one controlled command and retain its output for assertions."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - test-owned command vectors never invoke a shell.
        command,
        capture_output=True,
        check=False,
        cwd=current_directory,
        env=environment,
        text=True,
    )


def create_format_gate_repository(
    temporary_directory: Path,
    tracked_files: tuple[Path, ...] = DEFAULT_MARKDOWN_FILES,
) -> tuple[Path, tuple[Path, ...]]:
    """Create a Git repository with tracked, ignored, and untracked fixtures."""
    repository = temporary_directory / "repository"
    repository.mkdir()
    for source_path in tracked_files:
        _write_markdown(repository / source_path)
    for source_path in (
        Path("untracked.md"),
        Path("untracked.markdown"),
        Path("nested") / "untracked.mdx",
    ):
        _write_markdown(repository / source_path)
    (repository / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (repository / "ignored.md").write_text("unformatted\n", encoding="utf-8")
    _stage_markdown_sources(repository, tracked_files)
    return repository, tracked_files


def run_markdown_target(
    repository: Path,
    target: str,
    tools: MarkdownFormatterTools,
    options: MarkdownTargetOptions = DEFAULT_TARGET_OPTIONS,
) -> subprocess.CompletedProcess[str]:
    """Run a real Make target with controlled formatter executables."""
    make = shutil.which("make")
    if make is None:
        raise UnavailableTestDependencyError("make")
    command_stub = _write_successful_command(repository)
    overrides = (
        [] if options.discovery is None else [f"MDLINT_FILES_FIND={options.discovery}"]
    )
    spelling_overrides = (
        []
        if target != "markdownlint"
        else ["SPELLING_HELPER_TARGET=", "SPELLING_CONFIG_COMMAND=:", "TYPOS=true"]
    )
    target_environment = dict(options.environment or {})
    target_environment["MARKDOWN_FORMAT_CALL_LOG"] = str(tools.call_log)
    return run_process(
        [
            make,
            "-f",
            str(REPOSITORY_ROOT / "Makefile"),
            "--eval=ruff:",
            target,
            "VENV_TOOLS=",
            f"RUFF={command_stub}",
            f"CARGO={command_stub}",
            "RUST_DIR=.",
            f"MDTABLEFIX={tools.formatter}",
            f"MDLINT={tools.linter}",
            f"MAKE={make} -f {REPOSITORY_ROOT / 'Makefile'}",
            *overrides,
            *spelling_overrides,
        ],
        os.environ | target_environment,
        repository,
    )


def write_markdown_formatter_stubs(directory: Path) -> MarkdownFormatterTools:
    """Create controlled direct-mdtablefix and file-list markdownlint doubles."""
    call_log = directory / "markdown-format-calls.jsonl"
    formatter = directory / "mdtablefix"
    linter = directory / "markdownlint-cli2"
    formatter.write_text(_FORMATTER_STUB.replace("__PYTHON__", sys.executable))
    linter.write_text(_LINTER_STUB.replace("__PYTHON__", sys.executable))
    formatter.chmod(0o755)
    linter.chmod(0o755)
    return MarkdownFormatterTools(formatter, linter, call_log)


def read_calls(call_log: Path) -> list[MarkdownFormatterCall]:
    """Read a formatter transcript in the order Make invoked its stages."""
    return [
        _validate_formatter_call(json.loads(line))
        for line in call_log.read_text().splitlines()
    ]


def _validate_formatter_call(raw_call: object) -> MarkdownFormatterCall:
    """Validate the JSON schema emitted by the controlled formatter doubles."""
    mapping = _formatter_call_mapping(raw_call)
    call: MarkdownFormatterCall = {
        "tool": _formatter_call_string(mapping, "tool"),
        "paths": _formatter_call_strings(mapping, "paths"),
    }
    if "args" in mapping:
        call["args"] = _formatter_call_strings(mapping, "args")
    return call


def _formatter_call_mapping(raw_call: object) -> dict[object, object]:
    """Return the mapping form required by a formatter transcript entry."""
    if isinstance(raw_call, dict):
        return raw_call
    raise FormatterCallSchemaError("object")


def _formatter_call_string(mapping: dict[object, object], field: str) -> str:
    """Return one required string formatter-call field."""
    value = mapping.get(field)
    if isinstance(value, str):
        return value
    raise FormatterCallSchemaError(field)


def _formatter_call_strings(mapping: dict[object, object], field: str) -> list[str]:
    """Return one required list-of-strings formatter-call field."""
    value = mapping.get(field)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return list(value)
    raise FormatterCallSchemaError(field)


def _stage_markdown_sources(repository: Path, tracked_files: tuple[Path, ...]) -> None:
    """Initialize the Git index only after every tracked fixture exists."""
    git = shutil.which("git")
    if git is None:
        raise UnavailableTestDependencyError("Git")
    commands = (
        [git, "init", "-q"],
        [git, "add", "--", ".gitignore", *map(str, tracked_files)],
    )
    for command in commands:
        result = run_process(command, os.environ, repository)
        if result.returncode != 0:
            raise AssertionError(result.stdout + result.stderr)


def _write_markdown(source: Path) -> None:
    """Write the canonical fixture content, preserving its parent layout."""
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("unformatted\n", encoding="utf-8")


def _write_successful_command(directory: Path) -> Path:
    """Create a command double for unrelated formatter prerequisites."""
    executable = directory / "successful-command"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


_FORMATTER_STUB = textwrap.dedent(
    """\
    #!__PYTHON__
    import json
    import os
    import pathlib
    import subprocess
    import sys

    args = sys.argv[1:]
    required = ["--git", "--include-untracked", "--md-exts", "md,markdown,mdx"]
    if not all(flag in args for flag in required):
        raise SystemExit(64)
    paths = subprocess.check_output(
        [
            "git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
            "--", "*.md", "*.markdown", "*.mdx",
        ]
    ).split(b"\\0")[:-1]
    names = []
    for encoded in paths:
        name = os.fsdecode(encoded)
        path = pathlib.Path(name)
        if path.is_file() and not path.is_symlink():
            names.append(name)
    with pathlib.Path(os.environ["MARKDOWN_FORMAT_CALL_LOG"]).open("a") as log:
        call = {"tool": "mdtablefix", "paths": sorted(names), "args": args}
        print(json.dumps(call), file=log)
    if failure := os.environ.get("MARKDOWN_FORMAT_FORMATTER_FAILURE"):
        raise SystemExit(int(failure))
    if "--in-place" in args:
        for name in names:
            path = pathlib.Path(name)
            path.write_bytes(path.read_bytes().replace(b"unformatted", b"formatted"))
    """
)

_LINTER_STUB = textwrap.dedent(
    """\
    #!__PYTHON__
    import json
    import os
    import pathlib
    import sys

    args = sys.argv[1:]
    expects_fix = os.environ.get("MARKDOWN_FORMAT_EXPECT_LINTER_FIX") == "true"
    if expects_fix != (args[:1] == ["--fix"]):
        raise SystemExit(64)
    paths = args[1:] if expects_fix else args
    with pathlib.Path(os.environ["MARKDOWN_FORMAT_CALL_LOG"]).open("a") as log:
        call = {"tool": "markdownlint-cli2", "paths": sorted(paths)}
        print(json.dumps(call), file=log)
    if failure := os.environ.get("MARKDOWN_FORMAT_LINTER_FAILURE"):
        raise SystemExit(int(failure))
    if any(b"unformatted" in pathlib.Path(path).read_bytes() for path in paths):
        raise SystemExit(65)
    """
)
