"""Unit tests for SafeCmd execution-context integration.

Covers allowlist enforcement (success and forbidden-program rejection) and the
before/after hook contract: FIFO before-hook order, LIFO after-hook order,
command/result arguments passed to hooks, and cancellation skipping after
hooks.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ECHO, ForbiddenProgramError, ScopeConfig, scoped, sh
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd
    from tests.helpers.execution import ExecuteFn, _RunKwargs


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parametrised execution strategies for run() and run_sync()."""
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_run_raises_forbidden_when_program_not_in_allowlist(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() raises ForbiddenProgramError when program is not in context allowlist."""
    from cuprum.program import Program

    _, execute = execution_strategy
    command = sh.make(ECHO)("hello")
    # Create a context with an allowlist that does NOT include ECHO
    other_program = Program("cat")
    with (
        scoped(ScopeConfig(allowlist=frozenset([other_program]))),
        pytest.raises(ForbiddenProgramError, match=r"echo"),
    ):
        execute(command, {})


def test_run_succeeds_when_program_in_allowlist(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() succeeds when program is in context allowlist."""
    from cuprum.context import ScopeConfig, scoped

    _, execute = execution_strategy
    command = sh.make(ECHO)("-n", "allowed")
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        result = execute(command, {})
    assert result.exit_code == 0, "an allowlisted program should run and exit cleanly"
    assert result.stdout == "allowed", "the allowlisted command's output should capture"


def test_run_succeeds_with_empty_allowlist() -> None:
    """run() succeeds when context allowlist is empty (default permits all)."""
    # Default context has empty allowlist which permits all programs
    command = sh.make(ECHO)("-n", "hello")
    result = asyncio.run(command.run())
    assert result.exit_code == 0, "an empty allowlist should permit all programs"
    assert result.stdout == "hello", "the permitted command's output should be captured"


def test_run_invokes_before_hooks_in_fifo_order(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() invokes before hooks in registration order (FIFO)."""
    from cuprum.context import ScopeConfig, scoped

    _, execute = execution_strategy
    call_order: list[int] = []

    def hook1(cmd: SafeCmd) -> None:
        """Record this before hook as the first to run."""
        _ = cmd
        call_order.append(1)

    def hook2(cmd: SafeCmd) -> None:
        """Record this before hook as the second to run."""
        _ = cmd
        call_order.append(2)

    command = sh.make(ECHO)("-n", "hooks")
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]), before_hooks=(hook1, hook2))):
        execute(command, {})

    assert call_order == [1, 2], "before hooks should run in FIFO registration order"


def test_run_invokes_after_hooks_in_lifo_order(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() invokes after hooks in LIFO order (inner hooks run before outer)."""
    from cuprum.context import ScopeConfig, scoped

    _, execute = execution_strategy
    call_order: list[int] = []

    def outer_hook(cmd: SafeCmd, result: CommandResult) -> None:
        """Record the outer scope's after hook invocation."""
        _, _ = cmd, result
        call_order.append(1)

    def inner_hook(cmd: SafeCmd, result: CommandResult) -> None:
        """Record the inner scope's after hook invocation."""
        _, _ = cmd, result
        call_order.append(2)

    command = sh.make(ECHO)("-n", "hooks")
    # Nest scopes so the inner after hook runs before the outer (LIFO)
    with scoped(  # noqa: SIM117 — nested scopes required for LIFO hook ordering test
        ScopeConfig(allowlist=frozenset([ECHO]), after_hooks=(outer_hook,))
    ):
        with scoped(ScopeConfig(after_hooks=(inner_hook,))):
            execute(command, {})

    # Inner hook (2) runs first, then outer hook (1) - true LIFO semantics
    assert call_order == [2, 1], (
        "after hooks should run in LIFO order (inner scope before outer)"
    )


def test_run_passes_command_and_result_to_hooks(
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """run() passes SafeCmd to before hooks and SafeCmd+result to after hooks."""
    from cuprum.context import ScopeConfig, scoped

    _, execute = execution_strategy
    before_received: list[SafeCmd] = []
    after_received: list[tuple[SafeCmd, CommandResult]] = []

    def before_hook(cmd: SafeCmd) -> None:
        """Capture the command passed to the before hook."""
        before_received.append(cmd)

    def after_hook(cmd: SafeCmd, result: CommandResult) -> None:
        """Capture the command and result passed to the after hook."""
        after_received.append((cmd, result))

    command = sh.make(ECHO)("-n", "test")
    with scoped(
        ScopeConfig(
            allowlist=frozenset([ECHO]),
            before_hooks=(before_hook,),
            after_hooks=(after_hook,),
        )
    ):
        result = execute(command, {})

    assert len(before_received) == 1, "the before hook should be invoked exactly once"
    assert before_received[0] is command, (
        "the before hook should receive the executed command"
    )
    assert len(after_received) == 1, "the after hook should be invoked exactly once"
    assert after_received[0][0] is command, (
        "the after hook should receive the executed command"
    )
    assert after_received[0][1] is result, (
        "the after hook should receive the command result"
    )


def test_run_does_not_invoke_after_hooks_on_cancellation(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """run() does not invoke after hooks when task is cancelled."""
    from cuprum.context import ScopeConfig, scoped

    after_called = False

    def after_hook(cmd: SafeCmd, result: CommandResult) -> None:
        """Record that the after hook was invoked."""
        nonlocal after_called
        _, _ = cmd, result
        after_called = True

    # Use a long-running command that we can cancel
    command = python_builder("-c", "import time; time.sleep(10)")

    async def orchestrate() -> None:
        """Start the command under a scope, then cancel it."""
        with scoped(
            ScopeConfig(
                allowlist=frozenset([command.program]), after_hooks=(after_hook,)
            )
        ):
            task = asyncio.create_task(
                command.run(
                    output=RunOutputOptions(capture=False),
                    context=ExecutionContext(cancel_grace=0.1),
                ),
            )
            await asyncio.sleep(0.1)  # Let the process start
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(orchestrate())
    assert after_called is False, (
        "after hooks must not run when the command is cancelled"
    )
