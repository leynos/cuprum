"""Unit tests for SafeCmd baseline runtime execution.

Covers the default ``run()``/``run_sync()`` behaviour: capturing stdout,
stderr, and the exit code; overlaying environment variables without global
mutation; exposing the ``ok`` flag for non-zero exits; and honouring a
working-directory override.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ
from pathlib import Path

import pytest

from cuprum import (
    ECHO,
    _command_internals,
    _rusage,
    _subprocess_execution,
    _wait4_process,
    sh,
)
from cuprum.sh import CommandResult, ExecutionContext
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


_wait4_only = pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="asserts direct-child wait4 resource accounting",
)


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
    assert result.started_at > 0, "every command result must record a wall-clock start"
    assert result.duration >= 0, "every command result must report a duration"
    if sys.platform == "win32":
        assert result.max_rss_bytes is None, "Windows must not report child RSS"
        assert result.user_cpu_seconds is None, "Windows must not report child CPU"
        assert result.system_cpu_seconds is None, "Windows must not report child CPU"


@_wait4_only
def test_records_direct_child_resource_usage(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An isolated allocating child reports its own peak RSS and CPU usage."""
    allocation_bytes = 8 * 1024 * 1024
    command = python_builder(
        "-c",
        (
            f"allocation = bytearray({allocation_bytes}); "
            "allocation[::4096] = b'x' * len(allocation[::4096]); "
            "print('resource-probe')"
        ),
    )

    result = command.run_sync()

    assert result.max_rss_bytes is not None, "wait4 must expose direct-child RSS"
    assert result.max_rss_bytes >= allocation_bytes, (
        "direct-child RSS must include the touched allocation"
    )
    assert result.user_cpu_seconds is not None, "wait4 must expose child user CPU"
    assert result.user_cpu_seconds > 0, "allocating child must consume user CPU"
    assert result.system_cpu_seconds is not None, "wait4 must expose child system CPU"
    assert result.system_cpu_seconds >= 0, "child system CPU must be non-negative"


def test_publishes_cpu_deltas_from_direct_execution_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct execution publishes the CPU deltas from its rusage boundaries."""
    snapshots = [
        _rusage._ChildRusageSnapshot(0, 1.25, 2.5),
        _rusage._ChildRusageSnapshot(0, 4.75, 8.0),
    ]

    def capture_snapshot() -> _rusage._ChildRusageSnapshot:
        """Return the next controlled accounting boundary."""
        return snapshots.pop(0)

    monkeypatch.setattr(_wait4_process, "capture_child_rusage", capture_snapshot)
    monkeypatch.setattr(
        _wait4_process,
        "wait4_resource_measurement_available",
        lambda: False,
    )

    result = sh.make(ECHO)("resource-probe").run_sync()

    assert result.max_rss_bytes is None, "aggregate RSS cannot identify one child"
    assert result.user_cpu_seconds == pytest.approx(3.5), (
        "direct execution must publish the measured user CPU delta"
    )
    assert result.system_cpu_seconds == pytest.approx(5.5), (
        "direct execution must publish the measured system CPU delta"
    )


def test_records_start_times_before_subprocess_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct execution samples both clocks before awaiting subprocess spawn."""
    events: list[str] = []

    def monotonic_clock() -> float:
        """Record the monotonic start-time sample."""
        events.append("monotonic")
        return 10.0

    def wall_clock() -> float:
        """Record the wall-clock start-time sample."""
        events.append("wall")
        return 20.0

    async def fake_spawn(_: object) -> object:
        """Assert that both start-time samples precede spawning."""
        events.append("spawn")
        assert events == ["monotonic", "wall", "spawn"], (
            "direct start clocks must be sampled before subprocess spawn"
        )
        await asyncio.sleep(0)
        return type("Process", (), {"pid": 123})()

    async def fake_run_without_streams(
        _: object,
        __: object,
    ) -> tuple[int, float]:
        """Return a deterministic successful completion."""
        await asyncio.sleep(0)
        return 0, 13.0

    monkeypatch.setattr(_subprocess_execution.time, "perf_counter", monotonic_clock)
    # The observation builder installs ``time.time`` as the stage's
    # ``wall_clock`` callable, so the injected wall clock must be patched where
    # that attribute is read from — not in ``sh``, which no longer imports
    # ``time`` now that observation construction lives in ``_command_internals``.
    monkeypatch.setattr(_command_internals.time, "time", wall_clock)
    monkeypatch.setattr(_subprocess_execution, "_spawn_subprocess", fake_spawn)
    monkeypatch.setattr(
        _subprocess_execution,
        "_run_subprocess_without_streams",
        fake_run_without_streams,
    )

    result = asyncio.run(
        sh.make(ECHO)("quiet").run(output=sh.RunOutputOptions(capture=False)),
    )

    assert result.started_at == pytest.approx(20.0), (
        "direct result must retain the injected wall-clock start"
    )
    assert result.duration == pytest.approx(3.0), (
        "direct result duration must use the injected monotonic timestamps"
    )


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
