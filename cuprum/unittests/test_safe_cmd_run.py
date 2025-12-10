"""Unit tests for SafeCmd runtime execution."""

from __future__ import annotations

import asyncio
import io
import os
import sys
import time
import typing as typ
from pathlib import Path

import pytest

from cuprum import ECHO, sh
from cuprum.sh import CommandResult, ExecutionContext
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    from cuprum.sh import SafeCmd

type ExecuteFn = typ.Callable[[SafeCmd, dict[str, typ.Any]], CommandResult]


def _execute_async(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parametrised execution strategies for run() and run_sync()."""
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> typ.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
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


def test_captures_stderr_only(
    python_builder: typ.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() capture stderr independently."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("err", file=sys.stderr)',
    )

    result = execute(command, {})

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == ""
    assert result.stderr is not None
    assert result.stderr.strip() == "err"


def test_captures_and_echoes_stderr(
    python_builder: typ.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() echo stderr and capture it separately."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        'import sys; print("err", file=sys.stderr)',
    )

    result = execute(command, {"echo": True})

    captured = capsys.readouterr()

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == ""
    assert result.stderr is not None
    assert result.stderr.strip() == "err"
    assert captured.out == ""
    assert captured.err.strip() == "err"


def test_echoes_when_requested(
    capfd: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() echo output to stdout while still capturing it."""
    _, execute = execution_strategy
    command = sh.make(ECHO)("hello runtime")

    result = execute(command, {"echo": True})

    captured = capfd.readouterr()
    assert result.stdout is not None
    assert "hello runtime" in captured.out
    assert result.stdout.strip() == "hello runtime"


def test_applies_env_overrides(
    python_builder: typ.Callable[..., SafeCmd],
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
    python_builder: typ.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() capture non-zero exits and expose ok flag."""
    _, execute = execution_strategy
    command = python_builder("-c", "import sys; sys.exit(3)")

    result = execute(command, {})

    assert result.exit_code == 3
    assert result.ok is False


def test_applies_cwd_override(
    python_builder: typ.Callable[..., SafeCmd],
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


def test_allows_disabling_capture(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() execute without retaining output when disabled."""
    _, execute = execution_strategy
    command = sh.make(ECHO)("uncaptured output")

    result = execute(command, {"capture": False})

    assert result.exit_code == 0
    assert result.stdout is None
    assert result.stderr is None


def test_echoes_to_custom_sinks(
    python_builder: typ.Callable[..., SafeCmd],
    capsys: pytest.CaptureFixture[str],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() direct echo output to injected sinks."""
    _, execute = execution_strategy
    stdout_sink = io.StringIO()
    stderr_sink = io.StringIO()
    command = python_builder(
        "-c",
        'import sys; print("out"); print("err", file=sys.stderr)',
    )

    result = execute(
        command,
        {
            "echo": True,
            "context": ExecutionContext(
                stdout_sink=stdout_sink,
                stderr_sink=stderr_sink,
            ),
        },
    )
    captured = capsys.readouterr()

    assert result.stdout is not None
    assert result.stderr is not None
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert stdout_sink.getvalue().strip() == "out"
    assert stderr_sink.getvalue().strip() == "err"
    assert captured.out == ""
    assert captured.err == ""


def test_decodes_with_configured_encoding(
    python_builder: typ.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Both run() and run_sync() use configured encoding/errors for decoding."""
    _, execute = execution_strategy
    command = python_builder(
        "-c",
        ("import sys; sys.stdout.buffer.write(bytes([0x96])); sys.stdout.flush()"),
    )

    result = execute(
        command,
        {
            "context": ExecutionContext(
                encoding="cp1252",
                errors="strict",
            ),
        },
    )

    assert result.exit_code == 0
    assert result.ok is True
    assert result.stdout == "\u2013"
    assert result.stderr == ""


# -----------------------------------------------------------------------------
# Async-only tests (cancellation semantics)
# -----------------------------------------------------------------------------


def test_non_cooperative_subprocess_is_escalated_and_killed(
    tmp_path: Path,
    python_builder: typ.Callable[..., SafeCmd],
) -> None:
    """Non-cooperative child is killed after cancel_grace elapses."""
    if sys.platform == "win32":  # pragma: no cover - platform-specific behaviour
        pytest.skip("Cancellation escalation semantics rely on POSIX signals")
    script = tmp_path / "non_cooperative_child.py"
    pid_file = tmp_path / "nc.pid"
    script.write_text(
        "\n".join(
            (
                "import os",
                "import pathlib",
                "import signal",
                "import time",
                "pid_file = pathlib.Path(os.environ['CUPRUM_PID_FILE'])",
                "pid_file.write_text(str(os.getpid()))",
                "def _ignore(_signum, _frame):",
                "    pass",
                "signal.signal(signal.SIGTERM, _ignore)",
                "signal.signal(signal.SIGINT, _ignore)",
                "while True:",
                "    time.sleep(0.1)",
            ),
        ),
        encoding="utf-8",
    )

    command = python_builder(str(script))

    async def orchestrate() -> int:
        task = asyncio.create_task(
            command.run(
                capture=False,
                context=ExecutionContext(
                    env={"CUPRUM_PID_FILE": str(pid_file)},
                    cancel_grace=0.1,
                ),
            ),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive guard for CI slowness
            pytest.fail("PID file was not created within 5s")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pid_file.read_text())

    pid = asyncio.run(orchestrate())
    _poll_process_death(pid)


def _poll_process_death(pid: int, *, timeout: float = 1.0) -> None:
    """Poll for process exit within a timeout, failing if still alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)
    pytest.fail(f"Process {pid} still alive after {timeout}s of polling")


# -----------------------------------------------------------------------------
# Context integration tests (allowlist and hooks)
# -----------------------------------------------------------------------------


def test_run_raises_forbidden_when_program_not_in_allowlist(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() raises ForbiddenProgramError when program is not in context allowlist."""
    from cuprum.context import ForbiddenProgramError, scoped
    from cuprum.program import Program

    _, execute = execution_strategy
    command = sh.make(ECHO)("hello")
    # Create a context with an allowlist that does NOT include ECHO
    other_program = Program("cat")
    with (
        scoped(allowlist=frozenset([other_program])),
        pytest.raises(ForbiddenProgramError, match=r"echo"),
    ):
        execute(command, {})


def test_run_succeeds_when_program_in_allowlist(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() succeeds when program is in context allowlist."""
    from cuprum.context import scoped

    _, execute = execution_strategy
    command = sh.make(ECHO)("-n", "allowed")
    with scoped(allowlist=frozenset([ECHO])):
        result = execute(command, {})
    assert result.exit_code == 0
    assert result.stdout == "allowed"


def test_run_succeeds_with_empty_allowlist() -> None:
    """run() succeeds when context allowlist is empty (default permits all)."""
    # Default context has empty allowlist which permits all programs
    command = sh.make(ECHO)("-n", "hello")
    result = asyncio.run(command.run())
    assert result.exit_code == 0
    assert result.stdout == "hello"


def test_run_invokes_before_hooks_in_fifo_order(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() invokes before hooks in registration order (FIFO)."""
    from cuprum.context import scoped

    _, execute = execution_strategy
    call_order: list[int] = []

    def hook1(cmd: SafeCmd) -> None:
        _ = cmd
        call_order.append(1)

    def hook2(cmd: SafeCmd) -> None:
        _ = cmd
        call_order.append(2)

    command = sh.make(ECHO)("-n", "hooks")
    with scoped(allowlist=frozenset([ECHO]), before_hooks=(hook1, hook2)):
        execute(command, {})

    assert call_order == [1, 2]


def test_run_invokes_after_hooks_in_lifo_order(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() invokes after hooks in LIFO order (inner hooks run before outer)."""
    from cuprum.context import scoped

    _, execute = execution_strategy
    call_order: list[int] = []

    def outer_hook(cmd: SafeCmd, result: CommandResult) -> None:
        _, _ = cmd, result
        call_order.append(1)

    def inner_hook(cmd: SafeCmd, result: CommandResult) -> None:
        _, _ = cmd, result
        call_order.append(2)

    command = sh.make(ECHO)("-n", "hooks")
    # Nest scopes so the inner after hook runs before the outer (LIFO)
    with scoped(allowlist=frozenset([ECHO]), after_hooks=(outer_hook,)):  # noqa: SIM117
        with scoped(after_hooks=(inner_hook,)):
            execute(command, {})

    # Inner hook (2) runs first, then outer hook (1) - true LIFO semantics
    assert call_order == [2, 1]


def test_run_passes_command_and_result_to_hooks(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() passes SafeCmd to before hooks and SafeCmd+result to after hooks."""
    from cuprum.context import scoped

    _, execute = execution_strategy
    before_received: list[SafeCmd] = []
    after_received: list[tuple[SafeCmd, CommandResult]] = []

    def before_hook(cmd: SafeCmd) -> None:
        before_received.append(cmd)

    def after_hook(cmd: SafeCmd, result: CommandResult) -> None:
        after_received.append((cmd, result))

    command = sh.make(ECHO)("-n", "test")
    with scoped(
        allowlist=frozenset([ECHO]),
        before_hooks=(before_hook,),
        after_hooks=(after_hook,),
    ):
        result = execute(command, {})

    assert len(before_received) == 1
    assert before_received[0] is command
    assert len(after_received) == 1
    assert after_received[0][0] is command
    assert after_received[0][1] is result


def test_run_does_not_invoke_after_hooks_on_cancellation(
    python_builder: typ.Callable[..., SafeCmd],
) -> None:
    """run() does not invoke after hooks when task is cancelled."""
    from cuprum.context import scoped

    after_called = False

    def after_hook(cmd: SafeCmd, result: CommandResult) -> None:
        nonlocal after_called
        _, _ = cmd, result
        after_called = True

    # Use a long-running command that we can cancel
    command = python_builder("-c", "import time; time.sleep(10)")

    async def orchestrate() -> None:
        with scoped(allowlist=frozenset([command.program]), after_hooks=(after_hook,)):
            task = asyncio.create_task(
                command.run(capture=False, context=ExecutionContext(cancel_grace=0.1)),
            )
            await asyncio.sleep(0.1)  # Let the process start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(orchestrate())
    assert after_called is False
