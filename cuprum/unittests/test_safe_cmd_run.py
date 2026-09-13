"""Unit tests for SafeCmd baseline runtime execution.

Covers the default ``run()``/``run_sync()`` behaviour: capturing stdout,
stderr, and the exit code; overlaying environment variables without global
mutation; exposing the ``ok`` flag for non-zero exits; and honouring a
working-directory override.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ
from pathlib import Path

import pytest

from cuprum import ECHO, sh
from cuprum.sh import CommandResult, ExecutionContext
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def test_captures_output_and_exit_code(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() capture stdout/stderr and exit code by default."""
    _, execute = execution_strategy
    command = sh.make(ECHO)("-n", "hello")

    result = execute(command, {})

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == "hello"
    assert result.stderr == ""


def test_applies_env_overrides(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() overlay env vars without global mutation."""
    _, execute = execution_strategy
    env_var = "CUPRUM_TEST_ENV"
    original_value = os.environ.get(env_var)
    command = python_builder(
        "-c",
        f"import os;print(os.getenv('{env_var}'))",
    )

    result = execute(command, {"context": ExecutionContext(env={env_var: "present"})})

    assert result.stdout is not None
    assert result.stdout.strip() == "present"
    assert os.environ.get(env_var) == original_value, (
        "Environment overlays must not leak globally"
    )


def test_captures_nonzero_exit_code_and_ok_flag(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() capture non-zero exits and expose ok flag."""
    _, execute = execution_strategy
    command = python_builder("-c", "import sys; sys.exit(3)")

    result = execute(command, {})

    assert result.exit_code == 3
    assert result.ok is False


def test_applies_cwd_override(
    python_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() execute in the provided working directory."""
    _, execute = execution_strategy
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    command = python_builder("-c", "import os;print(os.getcwd())")

    result = execute(command, {"context": ExecutionContext(cwd=working_dir)})

    assert result.stdout is not None
    cwd_result = Path(result.stdout.strip())
    assert cwd_result == working_dir
