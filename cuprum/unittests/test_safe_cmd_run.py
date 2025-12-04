"""Unit tests for SafeCmd runtime execution."""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ
from pathlib import Path

import pytest

from cuprum import ECHO, sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.program import Program
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd


def _python_catalogue() -> ProgramCatalogue:
    """Construct a catalogue that allowlists the current Python executable."""
    python_program = Program(Path(sys.executable).as_posix())
    project = ProjectSettings(
        name="runtime-tests",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md#execution-runtime",),
        noise_rules=(),
    )
    return ProgramCatalogue(projects=(project,))


@pytest.fixture
def python_builder() -> typ.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    catalogue = _python_catalogue()
    program = next(iter(catalogue.allowlist))
    return sh.make(program, catalogue=catalogue)


def test_run_captures_output_and_exit_code() -> None:
    """run() captures stdout/stderr and exit code by default."""
    command = sh.make(ECHO)("-n", "hello")

    result = asyncio.run(command.run())

    assert result.exit_code == 0
    assert result.stdout == "hello"
    assert result.stderr == ""


def test_run_echoes_when_requested(capfd: pytest.CaptureFixture[str]) -> None:
    """run() can echo output to stdout while still capturing it."""
    command = sh.make(ECHO)("hello runtime")

    result = asyncio.run(command.run(echo=True))

    captured = capfd.readouterr()
    assert result.stdout is not None
    assert "hello runtime" in captured.out
    assert result.stdout.strip() == "hello runtime"


def test_run_applies_env_overrides(
    python_builder: typ.Callable[..., SafeCmd],
) -> None:
    """run() overlays provided env vars on top of the current environment."""
    env_var = "CUPRUM_TEST_ENV"
    command = python_builder(
        "-c",
        f"import os;print(os.getenv('{env_var}'))",
    )

    result = asyncio.run(
        command.run(context=ExecutionContext(env={env_var: "present"})),
    )

    assert result.stdout is not None
    assert result.stdout.strip() == "present"
    assert env_var not in os.environ, "Environment overlays must not leak globally"


def test_run_applies_cwd_override(
    python_builder: typ.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """run() executes in the provided working directory when supplied."""
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    command = python_builder("-c", "import os;print(os.getcwd())")

    result = asyncio.run(command.run(context=ExecutionContext(cwd=working_dir)))

    assert result.stdout is not None
    assert result.stdout.strip() == working_dir.as_posix()


def test_run_allows_disabling_capture() -> None:
    """run(capture=False) executes the command without retaining stdout/err."""
    command = sh.make(ECHO)("uncaptured output")

    result = asyncio.run(command.run(capture=False))

    assert result.exit_code == 0
    assert result.stdout is None
    assert result.stderr is None
