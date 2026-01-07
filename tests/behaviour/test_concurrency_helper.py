"""Behavioural tests for concurrent SafeCmd execution."""

from __future__ import annotations

import dataclasses as dc
import time
import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, scoped, sh
from cuprum.concurrent import ConcurrentResult, run_concurrent_sync
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.program import Program
    from cuprum.sh import SafeCmd


@scenario(
    "../features/concurrency_helper.feature",
    "Execute commands concurrently and collect results",
)
def test_execute_commands_concurrently() -> None:
    """Behavioural coverage for basic concurrent execution."""


@scenario(
    "../features/concurrency_helper.feature",
    "Concurrency limit restricts parallel execution",
)
def test_concurrency_limit_restricts_execution() -> None:
    """Behavioural coverage for concurrency limiting."""


@scenario(
    "../features/concurrency_helper.feature",
    "Failed commands are collected in results",
)
def test_failed_commands_collected() -> None:
    """Behavioural coverage for failure collection in collect-all mode."""


@scenario(
    "../features/concurrency_helper.feature",
    "Fail-fast cancels pending commands",
)
def test_fail_fast_cancels_pending() -> None:
    """Behavioural coverage for fail-fast cancellation."""


@dc.dataclass(frozen=True, slots=True)
class _ScenarioCommands:
    commands: tuple[SafeCmd, ...]
    allowlist: frozenset[Program]


@dc.dataclass(frozen=True, slots=True)
class _ConcurrentExecution:
    result: ConcurrentResult
    elapsed: float


@given("three quick echo commands", target_fixture="commands_under_test")
def given_three_quick_echo_commands() -> _ScenarioCommands:
    """Create three quick echo commands."""
    echo = sh.make(ECHO)
    commands = (
        echo("-n", "first"),
        echo("-n", "second"),
        echo("-n", "third"),
    )
    return _ScenarioCommands(commands=commands, allowlist=frozenset([ECHO]))


@given("four commands that sleep briefly", target_fixture="commands_under_test")
def given_four_sleeping_commands() -> _ScenarioCommands:
    """Create four commands that sleep briefly to test concurrency limiting."""
    _, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=python_catalogue()[0])
    commands = tuple(
        python("-c", "import time; time.sleep(0.1); print('done')") for _ in range(4)
    )
    return _ScenarioCommands(commands=commands, allowlist=frozenset([python_program]))


@given(
    "two succeeding and one failing command",
    target_fixture="commands_under_test",
)
def given_mixed_success_failure_commands() -> _ScenarioCommands:
    """Create commands where one fails."""
    _, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=python_catalogue()[0])
    commands = (
        python("-c", "print('success1')"),
        python("-c", "import sys; sys.exit(42)"),
        python("-c", "print('success2')"),
    )
    return _ScenarioCommands(commands=commands, allowlist=frozenset([python_program]))


@given(
    "a failing command and a slow command",
    target_fixture="commands_under_test",
)
def given_failing_and_slow_commands() -> _ScenarioCommands:
    """Create a failing command and a slow command for fail-fast testing."""
    _, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=python_catalogue()[0])
    commands = (
        python("-c", "import sys; sys.exit(1)"),
        python("-c", "import time; time.sleep(2); print('slow')"),
    )
    return _ScenarioCommands(commands=commands, allowlist=frozenset([python_program]))


@when("I run them concurrently", target_fixture="execution_result")
def when_run_concurrently(
    commands_under_test: _ScenarioCommands,
) -> _ConcurrentExecution:
    """Execute commands concurrently with default settings."""
    start = time.perf_counter()
    with scoped(allowlist=commands_under_test.allowlist):
        result = run_concurrent_sync(*commands_under_test.commands)
    elapsed = time.perf_counter() - start
    return _ConcurrentExecution(result=result, elapsed=elapsed)


@when(
    "I run them with concurrency limit 2",
    target_fixture="execution_result",
)
def when_run_with_concurrency_limit(
    commands_under_test: _ScenarioCommands,
) -> _ConcurrentExecution:
    """Execute commands with concurrency limit of 2."""
    start = time.perf_counter()
    with scoped(allowlist=commands_under_test.allowlist):
        result = run_concurrent_sync(*commands_under_test.commands, concurrency=2)
    elapsed = time.perf_counter() - start
    return _ConcurrentExecution(result=result, elapsed=elapsed)


@when(
    "I run them concurrently in collect-all mode",
    target_fixture="execution_result",
)
def when_run_collect_all(
    commands_under_test: _ScenarioCommands,
) -> _ConcurrentExecution:
    """Execute commands in collect-all mode (fail_fast=False)."""
    start = time.perf_counter()
    with scoped(allowlist=commands_under_test.allowlist):
        result = run_concurrent_sync(
            *commands_under_test.commands,
            fail_fast=False,
        )
    elapsed = time.perf_counter() - start
    return _ConcurrentExecution(result=result, elapsed=elapsed)


@when("I run them with fail-fast enabled", target_fixture="execution_result")
def when_run_fail_fast(
    commands_under_test: _ScenarioCommands,
) -> _ConcurrentExecution:
    """Execute commands with fail-fast enabled."""
    start = time.perf_counter()
    with scoped(allowlist=commands_under_test.allowlist):
        result = run_concurrent_sync(*commands_under_test.commands, fail_fast=True)
    elapsed = time.perf_counter() - start
    return _ConcurrentExecution(result=result, elapsed=elapsed)


@then("all results are returned in submission order")
def then_results_in_submission_order(execution_result: _ConcurrentExecution) -> None:
    """Verify results are returned in the order commands were submitted."""
    result = execution_result.result
    assert len(result.results) == 3
    assert result.results[0].stdout == "first"
    assert result.results[1].stdout == "second"
    assert result.results[2].stdout == "third"


@then("all commands succeeded")
def then_all_commands_succeeded(execution_result: _ConcurrentExecution) -> None:
    """Verify all commands completed successfully."""
    assert execution_result.result.ok is True
    assert execution_result.result.failures == ()


@then("all commands complete successfully")
def then_all_commands_complete_successfully(
    execution_result: _ConcurrentExecution,
) -> None:
    """Verify all commands completed successfully."""
    assert execution_result.result.ok is True
    assert len(execution_result.result.results) == 4


@then("execution respects the concurrency limit")
def then_execution_respects_limit(execution_result: _ConcurrentExecution) -> None:
    """Verify execution time reflects concurrency limit.

    With 4 commands of ~0.1s each and concurrency=2, expect ~0.2s elapsed.
    """
    # With concurrency=2 and 4 commands of 0.1s, expect at least 0.15s
    assert execution_result.elapsed >= 0.15, (
        f"Expected >= 0.15s with concurrency=2, got {execution_result.elapsed:.3f}s"
    )


@then("all three results are returned")
def then_all_three_results_returned(execution_result: _ConcurrentExecution) -> None:
    """Verify all three results are returned despite failure."""
    assert len(execution_result.result.results) == 3


@then("the failure index is reported")
def then_failure_index_reported(execution_result: _ConcurrentExecution) -> None:
    """Verify failure index is correctly reported."""
    result = execution_result.result
    assert result.ok is False
    assert result.failures == (1,)
    assert result.first_failure is not None
    assert result.first_failure.exit_code == 42


@then("partial results are returned quickly")
def then_partial_results_returned_quickly(
    execution_result: _ConcurrentExecution,
) -> None:
    """Verify partial results returned and slow command was cancelled."""
    # The slow command (2s) should have been cancelled, so elapsed < 1s
    assert execution_result.elapsed < 1.0, (
        f"Expected < 1.0s with fail-fast, got {execution_result.elapsed:.3f}s"
    )


@then("the first failure is accessible")
def then_first_failure_accessible(execution_result: _ConcurrentExecution) -> None:
    """Verify the first failure is accessible in the result."""
    result = execution_result.result
    assert result.ok is False
    assert result.first_failure is not None
    assert result.first_failure.exit_code == 1
