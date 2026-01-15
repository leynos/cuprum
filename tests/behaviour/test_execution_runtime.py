"""Behavioural tests for the SafeCmd execution runtime."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, TimeoutExpired, sh
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult, SafeCmd

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "Non-cooperative cancellation escalation relies on POSIX signals "
        "and os.kill semantics."
    ),
)


@scenario(
    "../features/execution_runtime.feature",
    "Run captures output by default",
)
def test_run_captures_output() -> None:
    """Behavioural coverage for default capture semantics."""


@scenario(
    "../features/execution_runtime.feature",
    "Cancellation terminates running subprocess",
)
def test_cancellation_terminates_subprocess() -> None:
    """Behavioural coverage for cancellation cleanup."""


@scenario(
    "../features/execution_runtime.feature",
    "Timeout terminates running subprocess",
)
def test_timeout_terminates_subprocess() -> None:
    """Behavioural coverage for timeout cleanup."""


@scenario(
    "../features/execution_runtime.feature",
    "Sync run captures output by default",
)
def test_sync_run_captures_output() -> None:
    """Behavioural coverage for sync capture semantics."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given("a simple safe echo command", target_fixture="simple_echo_command")
def given_simple_echo_command() -> SafeCmd:
    """Provide a SafeCmd that writes to stdout."""
    builder = sh.make(ECHO)
    return builder("-n", "behaviour")


@when(
    "I run the command asynchronously",
    target_fixture="run_result",
)
def when_run_command(simple_echo_command: SafeCmd) -> CommandResult:
    """Execute the SafeCmd using the async runtime."""
    return asyncio.run(simple_echo_command.run())


@when(
    "I run the command synchronously",
    target_fixture="run_result",
)
def when_run_command_sync(simple_echo_command: SafeCmd) -> CommandResult:
    """Execute the SafeCmd using the sync runtime."""
    return simple_echo_command.run_sync()


@then("the command result contains captured output")
def then_command_result_has_output(run_result: CommandResult) -> None:
    """Validate captured stdout/stderr and exit code."""
    assert run_result.exit_code == 0
    assert run_result.stdout == "behaviour"
    assert run_result.stderr == ""


@given(
    "a long running safe command",
    target_fixture="long_running_command",
)
def given_long_running_command(tmp_path: Path) -> dict[str, object]:
    """Construct a SafeCmd that blocks until cancelled."""
    return _create_worker_command(
        tmp_path,
        script_name="sleepy_worker.py",
        cooperative=True,
    )


@when("I cancel the command after it starts")
def when_cancel_command(
    behaviour_state: dict[str, object],
    long_running_command: dict[str, object],
) -> None:
    """Cancel the running command and record the child PID."""
    command = typ.cast("SafeCmd", long_running_command["command"])
    pid_file = typ.cast("Path", long_running_command["pid_file"])

    behaviour_state["pid"] = _cancel_command_with_grace(command, pid_file)
    behaviour_state["cleanup_context"] = "cancellation"


@when("I run the command with a timeout")
def when_run_command_with_timeout(
    behaviour_state: dict[str, object],
    long_running_command: dict[str, object],
) -> None:
    """Run a command with a timeout and record the error and PID."""
    command = typ.cast("SafeCmd", long_running_command["command"])
    pid_file = typ.cast("Path", long_running_command["pid_file"])
    timeout = 0.5

    ctx = ExecutionContext(env={"CUPRUM_PID_FILE": str(pid_file)})
    with pytest.raises(TimeoutExpired) as exc_info:
        command.run_sync(capture=False, timeout=timeout, context=ctx)

    behaviour_state["timeout_error"] = exc_info.value
    behaviour_state["timeout_value"] = timeout
    behaviour_state["pid"] = asyncio.run(_wait_for_pid(pid_file))
    behaviour_state["cleanup_context"] = "timeout"


@then("the subprocess stops cleanly")
def then_subprocess_stops_cleanly(behaviour_state: dict[str, object]) -> None:
    """Assert the subprocess has been terminated after cleanup."""
    pid = typ.cast("int", behaviour_state["pid"])
    context = typ.cast("str", behaviour_state.get("cleanup_context", "cancellation"))
    _wait_for_process_death(pid, timeout=5.0, context=context)


@then("a timeout error is raised")
def then_timeout_error_is_raised(behaviour_state: dict[str, object]) -> None:
    """Assert that a TimeoutExpired error is raised with expected settings."""
    error = typ.cast("TimeoutExpired", behaviour_state["timeout_error"])
    timeout_value = typ.cast("float", behaviour_state["timeout_value"])
    assert isinstance(error, TimeoutExpired)
    assert error.timeout == timeout_value


def _is_process_alive(pid: int) -> bool:
    """Return True when the pid exists on the host."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _create_worker_command(
    tmp_path: Path,
    *,
    script_name: str,
    cooperative: bool = True,
) -> dict[str, object]:
    """Create a SafeCmd-backed worker script.

    Set ``cooperative`` to False to install signal handlers that ignore
    termination signals instead of exiting cleanly.
    """
    script_path = tmp_path / script_name
    pid_file = tmp_path / f"{script_path.stem}.pid"
    signal_handler_body = (
        (
            "def _stop(_signum, _frame):",
            "    sys.exit(0)",
            "signal.signal(signal.SIGTERM, _stop)",
            "signal.signal(signal.SIGINT, _stop)",
        )
        if cooperative
        else (
            "def _ignore(_signum, _frame):",
            "    pass",
            "signal.signal(signal.SIGTERM, _ignore)",
            "signal.signal(signal.SIGINT, _ignore)",
        )
    )
    script_path.write_text(
        "\n".join(
            (
                "import os",
                "import pathlib",
                "import signal",
                "import sys",
                "import time",
                "pid_file = pathlib.Path(os.environ['CUPRUM_PID_FILE'])",
                "pid_file.write_text(str(os.getpid()))",
                *signal_handler_body,
                "while True:",
                "    time.sleep(1)",
            ),
        ),
        encoding="utf-8",
    )
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)(str(script_path))
    return {"command": command, "pid_file": pid_file}


def _wait_for_process_death(
    pid: int,
    *,
    timeout: float = 5.0,
    context: str = "subprocess termination",
) -> None:
    """Poll for process death until timeout, failing if still alive."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _is_process_alive(pid):
            return
        time.sleep(0.05)
    pytest.fail(  # pragma: no cover - defensive failure
        f"Process {pid} still alive after {context}",
    )


async def _wait_for_pid(pid_file: Path, timeout: float = 5.0) -> int:
    """Wait for the child process to publish its PID."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pid_file.exists():
            return int(pid_file.read_text().strip())
        await asyncio.sleep(0.05)
    msg = f"PID file was not created within {timeout}s"
    raise TimeoutError(msg)


def _cancel_command_with_grace(
    command: SafeCmd,
    pid_file: Path,
    *,
    cancel_grace: float | None = None,
) -> int:
    """Run a command, cancel it, and return the recorded child PID."""

    async def orchestrate() -> int:
        grace = (
            cancel_grace
            if cancel_grace is not None
            else ExecutionContext().cancel_grace
        )
        task = asyncio.create_task(
            command.run(
                capture=False,
                context=ExecutionContext(
                    env={"CUPRUM_PID_FILE": str(pid_file)},
                    cancel_grace=grace,
                ),
            ),
        )
        pid = await _wait_for_pid(pid_file)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    return asyncio.run(orchestrate())


@scenario(
    "../features/execution_runtime.feature",
    "Cancellation escalates a non-cooperative subprocess",
)
def test_cancellation_escalates_non_cooperative() -> None:
    """Behavioural coverage for escalation after cancel grace."""


@given(
    "a non-cooperative safe command",
    target_fixture="non_cooperative_command",
)
def given_non_cooperative_command(tmp_path: Path) -> dict[str, object]:
    """Construct a SafeCmd that ignores termination signals."""
    return _create_worker_command(
        tmp_path,
        script_name="stubborn_worker.py",
        cooperative=False,
    )


@when("I cancel the command with a short grace period")
def when_cancel_non_cooperative(
    behaviour_state: dict[str, object],
    non_cooperative_command: dict[str, object],
) -> None:
    """Cancel a non-cooperative command and record its PID."""
    command = typ.cast("SafeCmd", non_cooperative_command["command"])
    pid_file = typ.cast("Path", non_cooperative_command["pid_file"])

    behaviour_state["pid"] = _cancel_command_with_grace(
        command,
        pid_file,
        cancel_grace=0.1,
    )


@then("the subprocess is killed after escalation")
def then_subprocess_killed_after_escalation(
    behaviour_state: dict[str, object],
) -> None:
    """Assert that a stubborn subprocess is eventually killed."""
    pid = typ.cast("int", behaviour_state["pid"])
    _wait_for_process_death(
        pid,
        timeout=5.0,
        context="escalation after SIGTERM ignored",
    )
