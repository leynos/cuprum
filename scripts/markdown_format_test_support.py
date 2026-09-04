"""Support the process-boundary Markdown formatting gate tests."""

from __future__ import annotations

import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - the process boundary is under test.
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPOSITORY_ROOT / "scripts" / "check-markdown-format.sh"

_DEFAULT_TRACKED_FILES = (
    Path("guide.md"),
    Path("nested") / "tracked with spaces.md",
    Path("nested") / "tracked\nwith newline.md",
)


class _UnavailableTestDependencyError(RuntimeError):
    """Report a missing external dependency for the integration test setup."""

    def __init__(self, dependency: str) -> None:
        self._dependency = dependency

    def __str__(self) -> str:
        return f"the gate integration test requires {self._dependency}"


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
        [git, "add", ".gitignore", *(str(path) for path in tracked_files)],
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
    make = shutil.which("make")
    if make is None:
        raise _UnavailableTestDependencyError("make")
    command_stub = write_successful_command(repository)
    discovery_override = (
        [] if markdown_discovery is None else [f"MD_FILES_FIND={markdown_discovery}"]
    )
    return run_process(
        [
            make,
            "-f",
            str(REPOSITORY_ROOT / "Makefile"),
            "check-fmt",
            "VENV_TOOLS=",
            f"RUFF={command_stub}",
            f"CARGO={command_stub}",
            "RUST_DIR=.",
            *discovery_override,
        ],
        os.environ | {"MARKDOWN_CHECKER_CALL_LOG": str(checker_log)},
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
