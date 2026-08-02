"""Behavioural tests for the SafeCmd execution runtime."""

from __future__ import annotations

import asyncio
import sys
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, TimeoutExpired, sh
from cuprum.sh import ExecutionContext, RunOutputOptions, StdinInput
from tests.behaviour._execution_runtime_support import (
    WorkerCommand,
    _cancel_command_with_grace,
    _create_worker_command,
    _wait_for_pid,
    _wait_for_process_death,
)
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


@scenario(
    "../features/execution_runtime.feature",
    "Run passes direct stdin input to the command",
)
def test_run_passes_direct_stdin_input() -> None:
    """Behavioural coverage for direct stdin injection."""


@pytest.fixture
def behaviour_state() -> dict[str, object]:
    """Shared mutable state for behaviour scenarios."""
    return {}


@given("a simple safe echo command", target_fixture="simple_echo_command")
def given_simple_echo_command() -> SafeCmd:
    """Provide a SafeCmd that writes to stdout."""
    builder = sh.make(ECHO)
    return builder("-n", "behaviour")


@given("a safe command that reads stdin", target_fixture="stdin_reader_command")
def given_stdin_reader_command() -> SafeCmd:
    """Provide a SafeCmd that echoes stdin to stdout."""
    catalogue, python_program = python_catalogue()
    return sh.make(python_program, catalogue=catalogue)(
        "-c",
        "import sys; print(sys.stdin.read(), end='')",
    )


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


@when(
    "I run the command with direct stdin text",
    target_fixture="run_result",
)
def when_run_command_with_direct_stdin(stdin_reader_command: SafeCmd) -> CommandResult:
    """Execute the SafeCmd with text fed directly to stdin."""
    return stdin_reader_command.run_sync(stdin=StdinInput(text="behaviour stdin\n"))


@then("the command result contains captured output")
def then_command_result_has_output(run_result: CommandResult) -> None:
    """Validate captured stdout/stderr and exit code."""
    assert run_result.exit_code == 0
    assert run_result.stdout == "behaviour"
    assert not run_result.stderr


@then("the command result contains the stdin text")
def then_command_result_has_stdin_text(run_result: CommandResult) -> None:
    """Validate captured stdout from direct stdin input."""
    assert run_result.exit_code == 0
    assert run_result.stdout == "behaviour stdin\n"
    assert not run_result.stderr


@given(
    "a long running safe command",
    target_fixture="long_running_command",
)
def given_long_running_command(tmp_path: Path) -> WorkerCommand:
    """Construct a SafeCmd that blocks until cancelled."""
    return _create_worker_command(
        tmp_path,
        script_name="sleepy_worker.py",
        cooperative=True,
    )


@when("I cancel the command after it starts")
def when_cancel_command(
    behaviour_state: dict[str, object],
    long_running_command: WorkerCommand,
) -> None:
    """Cancel the running command and record the child PID."""
    command = long_running_command["command"]
    pid_file = long_running_command["pid_file"]

    behaviour_state["pid"] = _cancel_command_with_grace(command, pid_file)
    behaviour_state["cleanup_context"] = "cancellation"


@when("I run the command with a timeout")
def when_run_command_with_timeout(
    behaviour_state: dict[str, object],
    long_running_command: WorkerCommand,
) -> None:
    """Run a command with a timeout and record the error and PID."""
    command = long_running_command["command"]
    pid_file = long_running_command["pid_file"]
    timeout = 0.5

    ctx = ExecutionContext(env={"CUPRUM_PID_FILE": str(pid_file)})
    with pytest.raises(TimeoutExpired, match=r"timed out") as exc_info:
        command.run_sync(
            output=RunOutputOptions(capture=False),
            timeout=timeout,
            context=ctx,
        )

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
def given_non_cooperative_command(tmp_path: Path) -> WorkerCommand:
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
