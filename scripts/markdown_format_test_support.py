"""Support the process-boundary Markdown formatting gate tests."""

from __future__ import annotations

import dataclasses as dc
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the process boundary is under test.
import sys
import textwrap
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check-markdown-format.sh"

_DEFAULT_TRACKED_FILES = (
    Path("guide.md"),
    Path("guide.markdown"),
    Path("guide.mdx"),
    Path("nested") / "tracked with spaces.md",
    Path("nested") / "tracked\nwith newline.md",
)


class _UnavailableTestDependencyError(RuntimeError):
    """Report a missing external dependency for the integration test setup."""

    def __init__(self, dependency: str) -> None:
        """Record which dependency the integration test could not find."""
        self._dependency = dependency

    def __str__(self) -> str:
        """Name the missing dependency in the failure message."""
        return f"the gate integration test requires {self._dependency}"


@dc.dataclass(frozen=True, slots=True)
class _MarkdownFormatterTools:
    """Keep the controlled formatter boundary reusable only by Markdown Make tests."""

    formatter: Path
    linter: Path
    call_log: Path


@dc.dataclass(frozen=True, slots=True)
class _MarkdownMakeGate:
    """Describe one controlled Markdown Make invocation for these gate tests only."""

    target: str
    environment: cabc.Mapping[str, str]
    mdtablefix: Path | None = None
    markdownlint: Path | None = None


def run_process(
    command: list[str],
    environment: cabc.Mapping[str, str],
    current_directory: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a controlled process with captured output."""
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - executes the controlled fixture.
        command,
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        cwd=current_directory,
    )


def create_format_gate_repository(
    temporary_directory: Path,
    tracked_files: tuple[Path, ...] = _DEFAULT_TRACKED_FILES,
) -> tuple[Path, tuple[Path, ...], Path]:
    """Create tracked, ignored, and untracked Markdown sources for the gate."""
    repository = temporary_directory / "repository"
    repository.mkdir()
    for path in tracked_files:
        source = repository / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("formatted\n", encoding="utf-8")
    (repository / ".gitignore").write_text("ignored.md\n", encoding="utf-8")
    (repository / "ignored.md").write_text("formatted\n", encoding="utf-8")
    (repository / "untracked.md").write_text("formatted\n", encoding="utf-8")
    (repository / "ruff").touch()
    write_markdown_checker_stub(repository)
    return repository, tracked_files, repository / "checker-paths.bin"


def stage_markdown_sources(repository: Path, tracked_files: tuple[Path, ...]) -> None:
    """Initialize a Git index containing the formatter's Markdown inputs."""
    git = shutil.which("git")
    if git is None:
        raise _UnavailableTestDependencyError("Git")
    initialized = run_process([git, "init", "-q"], os.environ, repository)
    if initialized.returncode != 0:
        raise AssertionError(initialized.stdout + initialized.stderr)
    added = run_process(
        [git, "add", "--", ".gitignore", *(str(path) for path in tracked_files)],
        os.environ,
        repository,
    )
    if added.returncode != 0:
        raise AssertionError(added.stdout + added.stderr)


def run_check_format_gate(
    repository: Path,
    checker_log: Path,
    markdown_discovery: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real formatting recipe against the controlled repository."""
    return _run_markdown_make_gate(
        repository,
        _MarkdownMakeGate(
            target="check-fmt",
            environment=os.environ | {"MARKDOWN_CHECKER_CALL_LOG": str(checker_log)},
        ),
        markdown_discovery,
    )


def run_format_gate(
    repository: Path,
    tools: _MarkdownFormatterTools,
    markdown_discovery: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real formatter recipe against controlled Markdown tools."""
    return _run_markdown_make_gate(
        repository,
        _MarkdownMakeGate(
            target="fmt",
            environment=os.environ | {"MARKDOWN_FORMAT_CALL_LOG": str(tools.call_log)},
            mdtablefix=tools.formatter,
            markdownlint=tools.linter,
        ),
        markdown_discovery,
    )


def _run_markdown_make_gate(
    repository: Path,
    gate: _MarkdownMakeGate,
    markdown_discovery: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a controlled Markdown Make target; reuse only from this test support."""
    make = shutil.which("make")
    if make is None:
        raise _UnavailableTestDependencyError("make")
    command_stub = write_successful_command(repository)
    discovery_override = (
        [] if markdown_discovery is None else [f"MD_FILES_FIND={markdown_discovery}"]
    )
    mdtablefix = gate.mdtablefix or command_stub
    markdownlint = [] if gate.markdownlint is None else [f"MDLINT={gate.markdownlint}"]
    return run_process(
        [
            make,
            "-f",
            str(REPOSITORY_ROOT / "Makefile"),
            gate.target,
            "VENV_TOOLS=",
            f"RUFF={command_stub}",
            f"CARGO={command_stub}",
            "RUST_DIR=.",
            f"MDTABLEFIX={mdtablefix}",
            *markdownlint,
            *discovery_override,
        ],
        gate.environment,
        repository,
    )


def write_successful_command(directory: Path) -> Path:
    """Create a command stub that accepts every argument."""
    executable = directory / "successful-command"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def write_markdown_checker_stub(directory: Path) -> Path:
    """Create a checker stub that records its NUL-delimited path arguments."""
    scripts_directory = directory / "scripts"
    scripts_directory.mkdir()
    checker = scripts_directory / "check-markdown-format.sh"
    checker.write_text(
        '#!/bin/sh\nprintf \'%s\\0\' "$@" > "$MARKDOWN_CHECKER_CALL_LOG"\n',
        encoding="utf-8",
    )
    checker.chmod(0o755)
    return checker


def write_markdown_formatter_stubs(directory: Path) -> _MarkdownFormatterTools:
    """Create formatter and linter doubles that record their ordered inputs."""
    call_log = directory / "markdown-format-calls.jsonl"
    formatter = directory / "mdtablefix"
    linter = directory / "markdownlint-cli2"
    formatter.write_text(
        textwrap.dedent(
            """\
            #!__PYTHON__
            import json
            import os
            import pathlib
            import sys

            flags = [
                "--in-place",
                "--wrap",
                "--renumber",
                "--breaks",
                "--ellipsis",
                "--fences",
            ]
            arguments = sys.argv[1:]
            if arguments[:len(flags)] != flags:
                raise SystemExit(64)
            paths = arguments[len(flags):]
            call_log = pathlib.Path(os.environ["MARKDOWN_FORMAT_CALL_LOG"])
            with call_log.open("a") as log:
                print(json.dumps({"tool": "mdtablefix", "paths": paths}), file=log)
            for path in paths:
                source = pathlib.Path(path)
                source.write_bytes(
                    source.read_bytes().replace(b"unformatted", b"formatted")
                )
            """
        ).replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    linter.write_text(
        textwrap.dedent(
            """\
            #!__PYTHON__
            import json
            import os
            import pathlib
            import sys

            arguments = sys.argv[1:]
            if arguments[:1] != ["--fix"]:
                raise SystemExit(64)
            paths = arguments[1:]
            if any(b"unformatted" in pathlib.Path(path).read_bytes() for path in paths):
                raise SystemExit(65)
            call_log = pathlib.Path(os.environ["MARKDOWN_FORMAT_CALL_LOG"])
            with call_log.open("a") as log:
                entry = {"tool": "markdownlint-cli2", "paths": paths}
                print(json.dumps(entry), file=log)
            """
        ).replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    formatter.chmod(0o755)
    linter.chmod(0o755)
    return _MarkdownFormatterTools(formatter, linter, call_log)
