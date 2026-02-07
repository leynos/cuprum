"""Unit tests for concurrent SafeCmd execution."""

from __future__ import annotations

import asyncio
import dataclasses
import time
import typing as typ

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

from cuprum import ECHO, ForbiddenProgramError, ScopeConfig, scoped, sh
from cuprum.concurrent import (
    ConcurrentConfig,
    ConcurrentResult,
    run_concurrent,
    run_concurrent_sync,
)
from tests.helpers.catalogue import python_catalogue


@dataclasses.dataclass(frozen=True, slots=True)
class _TimingExpectation:
    """Expected timing bounds for concurrent command execution.

    Attributes
    ----------
        min_elapsed: Minimum expected elapsed time in seconds. The test will
            fail if execution completes faster than this threshold.
        max_elapsed: Maximum expected elapsed time in seconds (optional). If
            provided, the test will fail if execution takes longer than this.

    """

    min_elapsed: float
    max_elapsed: float | None = None


def _assert_concurrent_timing(
    num_commands: int,
    sleep_seconds: float,
    concurrency: int | None,
    timing: _TimingExpectation,
) -> None:
    """Run concurrent sleep commands and assert timing constraints.

    Creates `num_commands` Python commands that each sleep for `sleep_seconds`,
    runs them concurrently with the specified concurrency setting, and asserts
    that the elapsed time falls within the expected bounds.

    Parameters
    ----------
    num_commands:
        Number of sleep commands to run.
    sleep_seconds:
        Duration each command sleeps for.
    concurrency:
        Concurrency limit (None for unlimited).
    timing:
        Expected timing bounds for the concurrent execution.

    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    commands = [
        python("-c", f"import time; time.sleep({sleep_seconds}); print('done')")
        for _ in range(num_commands)
    ]

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        start = time.perf_counter()
        result = run_concurrent_sync(
            *commands, config=ConcurrentConfig(concurrency=concurrency)
        )
        elapsed = time.perf_counter() - start

    assert result.ok is True
    assert len(result.results) == num_commands
    assert elapsed >= timing.min_elapsed, (
        f"Expected >= {timing.min_elapsed}s with concurrency={concurrency}, "
        f"got {elapsed:.3f}s"
    )
    if timing.max_elapsed is not None:
        assert elapsed < timing.max_elapsed, (
            f"Expected < {timing.max_elapsed}s with concurrency={concurrency}, "
            f"got {elapsed:.3f}s"
        )


def _run_hook_test[T](
    hook_context: cabc.Callable[[list[T]], contextlib.AbstractContextManager[None]],
    num_commands: int = 3,
) -> list[T]:
    """Run concurrent echo commands within a hook context and return hook calls.

    Creates `num_commands` echo commands, runs them concurrently within the
    provided hook context, and returns the list populated by the hook.

    Parameters
    ----------
    hook_context:
        A factory that takes an empty list and returns a context manager.
        The hook implementation should append to the list.
    num_commands:
        Number of echo commands to run (default: 3).

    Returns
    -------
    list[T]
        The list of values collected by the hook during execution.

    """
    echo = sh.make(ECHO)
    commands = [echo("-n", f"cmd{i}") for i in range(num_commands)]
    hook_calls: list[T] = []

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), hook_context(hook_calls):
        run_concurrent_sync(*commands)

    return hook_calls


def test_run_concurrent_returns_concurrent_result() -> None:
    """run_concurrent returns a ConcurrentResult with results tuple."""
    echo = sh.make(ECHO)
    cmd1 = echo("-n", "one")
    cmd2 = echo("-n", "two")

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        result = run_concurrent_sync(cmd1, cmd2)

    assert isinstance(result, ConcurrentResult)
    assert len(result.results) == 2
    assert result.ok is True
    assert result.failures == ()


def test_run_concurrent_preserves_submission_order() -> None:
    """Results are returned in the order commands were submitted."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    # Commands with different outputs to verify order
    cmd1 = python("-c", "print('first')")
    cmd2 = python("-c", "print('second')")
    cmd3 = python("-c", "print('third')")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = run_concurrent_sync(cmd1, cmd2, cmd3)

    assert len(result.results) == 3
    assert result.results[0].stdout is not None
    assert "first" in result.results[0].stdout
    assert result.results[1].stdout is not None
    assert "second" in result.results[1].stdout
    assert result.results[2].stdout is not None
    assert "third" in result.results[2].stdout


def test_run_concurrent_sync_mirrors_async() -> None:
    """run_concurrent_sync produces identical results to async version."""
    echo = sh.make(ECHO)
    cmd = echo("-n", "hello")

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        sync_result = run_concurrent_sync(cmd)
        async_result = asyncio.run(run_concurrent(cmd))

    assert sync_result.ok == async_result.ok
    assert len(sync_result.results) == len(async_result.results)
    assert sync_result.results[0].stdout == async_result.results[0].stdout


def test_concurrency_limit_restricts_parallel_execution() -> None:
    """Concurrency limit restricts the number of parallel executions."""
    # Use longer sleeps and conservative thresholds to avoid flakiness under CI load
    _assert_concurrent_timing(
        num_commands=4,
        sleep_seconds=0.2,
        concurrency=2,
        timing=_TimingExpectation(min_elapsed=0.3),
    )


def test_concurrency_none_allows_unlimited() -> None:
    """concurrency=None allows all commands to run in parallel."""
    # Keep a relaxed upper bound so this remains stable under xdist/CI jitter
    # while still proving the run is significantly below sequential timing.
    _assert_concurrent_timing(
        num_commands=4,
        sleep_seconds=0.2,
        concurrency=None,
        timing=_TimingExpectation(min_elapsed=0.0, max_elapsed=0.8),
    )


def test_concurrency_one_executes_sequentially() -> None:
    """concurrency=1 executes commands sequentially."""
    # Use longer sleeps for more reliable sequential timing detection
    _assert_concurrent_timing(
        num_commands=3,
        sleep_seconds=0.15,
        concurrency=1,
        timing=_TimingExpectation(min_elapsed=0.35),
    )


def test_collect_all_mode_continues_after_failure() -> None:
    """Collect-all mode (default) continues executing after a failure."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    cmd1 = python("-c", "print('first')")
    cmd2 = python("-c", "import sys; sys.exit(1)")  # Fails
    cmd3 = python("-c", "print('third')")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = run_concurrent_sync(
            cmd1, cmd2, cmd3, config=ConcurrentConfig(fail_fast=False)
        )

    assert result.ok is False
    assert len(result.results) == 3
    assert result.failures == (1,)
    assert result.results[0].ok is True
    assert result.results[1].ok is False
    assert result.results[2].ok is True


def test_fail_fast_mode_cancels_pending() -> None:
    """Fail-fast mode cancels pending commands after first failure."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    # First command fails immediately, second sleeps
    cmd1 = python("-c", "import sys; sys.exit(42)")
    cmd2 = python("-c", "import time; time.sleep(1); print('should not complete')")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        start = time.perf_counter()
        result = run_concurrent_sync(
            cmd1, cmd2, config=ConcurrentConfig(fail_fast=True)
        )
        elapsed = time.perf_counter() - start

    assert result.ok is False
    # The slow command should be cancelled, so elapsed time should be short
    assert elapsed < 0.5, f"Expected < 0.5s with fail-fast, got {elapsed:.3f}s"

    # Verify shape of results and failures
    # At minimum cmd1 completed (failed); cmd2 may or may not be in results
    assert len(result.results) >= 1, "At least the failed command should be in results"
    assert result.failures == (0,), "First result should be the failure"
    assert result.first_failure is not None
    assert result.first_failure is result.results[0]
    assert result.first_failure.exit_code == 42


def test_failures_tuple_contains_correct_indices() -> None:
    """The failures tuple contains indices of failed commands in order."""
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    cmd0 = python("-c", "print('ok')")
    cmd1 = python("-c", "import sys; sys.exit(1)")  # Fails
    cmd2 = python("-c", "print('ok')")
    cmd3 = python("-c", "import sys; sys.exit(2)")  # Fails

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = run_concurrent_sync(cmd0, cmd1, cmd2, cmd3)

    assert result.failures == (1, 3)
    assert result.first_failure is result.results[1]


def test_before_hooks_fire_per_command() -> None:
    """Before hooks fire for each command in concurrent execution."""
    from cuprum import SafeCmd, before

    def make_tracker(calls: list[str]) -> cabc.Callable[[SafeCmd], None]:
        def track_before(cmd: SafeCmd) -> None:
            calls.append(str(cmd.program))

        return track_before

    hook_calls = _run_hook_test(lambda calls: before(make_tracker(calls)))

    assert len(hook_calls) == 3
    assert all(call == "echo" for call in hook_calls)


def test_after_hooks_fire_per_command() -> None:
    """After hooks fire for each command in concurrent execution."""
    from cuprum import CommandResult, SafeCmd, after

    def make_tracker(calls: list[int]) -> cabc.Callable[[SafeCmd, CommandResult], None]:
        def track_after(cmd: SafeCmd, result: CommandResult) -> None:
            _ = cmd  # Unused
            calls.append(result.exit_code)

        return track_after

    hook_calls = _run_hook_test(lambda calls: after(make_tracker(calls)))

    assert len(hook_calls) == 3
    assert all(code == 0 for code in hook_calls)


def test_observe_hooks_receive_events_per_command() -> None:
    """Observe hooks receive events for each command."""
    from cuprum import ExecEvent, observe

    echo = sh.make(ECHO)
    commands = [echo("-n", f"cmd{i}") for i in range(2)]
    events: list[ExecEvent] = []

    def track_events(ev: ExecEvent) -> None:
        events.append(ev)

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))), observe(track_events):
        run_concurrent_sync(*commands)

    # Each command should emit plan, start, stdout (if any), exit events
    phases = [ev.phase for ev in events]
    assert phases.count("plan") == 2
    assert phases.count("start") == 2
    assert phases.count("exit") == 2


def test_empty_commands_raises_value_error() -> None:
    """Calling run_concurrent with no commands raises ValueError."""
    with pytest.raises(ValueError, match="At least one command"):
        run_concurrent_sync()


def test_concurrency_zero_raises_value_error() -> None:
    """Concurrency of 0 raises ValueError."""
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        ConcurrentConfig(concurrency=0)


def test_concurrency_negative_raises_value_error() -> None:
    """Negative concurrency raises ValueError."""
    with pytest.raises(ValueError, match="concurrency must be >= 1"):
        ConcurrentConfig(concurrency=-1)


def test_forbidden_program_raises_before_execution() -> None:
    """ForbiddenProgramError is raised before any command executes."""
    from cuprum import LS

    echo = sh.make(ECHO)
    cmd1 = echo("-n", "hello")
    cmd2 = echo("-n", "world")

    # Allowlist only LS, so ECHO is forbidden
    forbidden_ctx = scoped(ScopeConfig(allowlist=frozenset([LS])))
    with forbidden_ctx, pytest.raises(ForbiddenProgramError):
        run_concurrent_sync(cmd1, cmd2)


def test_concurrent_result_ok_property() -> None:
    """ConcurrentResult.ok returns True only when all commands succeed."""
    from cuprum import CommandResult, Program

    # All success
    result_ok = ConcurrentResult(
        results=(
            CommandResult(Program("echo"), (), 0, 1, "out", ""),
            CommandResult(Program("echo"), (), 0, 2, "out", ""),
        ),
        failures=(),
    )
    assert result_ok.ok is True

    # One failure
    result_fail = ConcurrentResult(
        results=(
            CommandResult(Program("echo"), (), 0, 1, "out", ""),
            CommandResult(Program("echo"), (), 1, 2, "out", ""),
        ),
        failures=(1,),
    )
    assert result_fail.ok is False


def test_concurrent_result_first_failure_property() -> None:
    """ConcurrentResult.first_failure returns the first failed result."""
    from cuprum import CommandResult, Program

    result1 = CommandResult(Program("echo"), (), 0, 1, "out", "")
    result2 = CommandResult(Program("echo"), (), 1, 2, "out", "")
    result3 = CommandResult(Program("echo"), (), 2, 3, "out", "")

    concurrent_result = ConcurrentResult(
        results=(result1, result2, result3),
        failures=(1, 2),
    )

    assert concurrent_result.first_failure is result2

    # No failures
    ok_result = ConcurrentResult(results=(result1,), failures=())
    assert ok_result.first_failure is None


def test_single_command_works() -> None:
    """run_concurrent works with a single command."""
    echo = sh.make(ECHO)
    cmd = echo("-n", "solo")

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        result = run_concurrent_sync(cmd)

    assert result.ok is True
    assert len(result.results) == 1
    assert result.results[0].stdout == "solo"


def test_capture_false_returns_none_stdout() -> None:
    """When capture=False, stdout is None in results."""
    echo = sh.make(ECHO)
    cmd = echo("-n", "hello")

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        result = run_concurrent_sync(cmd, config=ConcurrentConfig(capture=False))

    assert result.ok is True
    assert result.results[0].stdout is None


def test_async_run_concurrent() -> None:
    """run_concurrent works correctly as an async function."""
    echo = sh.make(ECHO)
    cmd1 = echo("-n", "async1")
    cmd2 = echo("-n", "async2")

    async def exercise() -> ConcurrentResult:
        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
            return await run_concurrent(cmd1, cmd2)

    result = asyncio.run(exercise())

    assert result.ok is True
    assert len(result.results) == 2


def test_fail_fast_first_failure_indexes_correctly_when_earlier_cancelled() -> None:
    """first_failure indexes correctly when earlier commands are cancelled.

    When fail-fast cancels earlier (slower) commands, the failures tuple must
    use remapped indices into the compacted results array, not original
    submission indices. This test ensures first_failure doesn't raise IndexError
    or return the wrong result when a later command fails first.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)

    # cmd0 sleeps (will be cancelled), cmd1 fails immediately
    cmd0 = python("-c", "import time; time.sleep(2); print('slow')")
    cmd1 = python("-c", "import sys; sys.exit(99)")

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        start = time.perf_counter()
        result = run_concurrent_sync(
            cmd0, cmd1, config=ConcurrentConfig(fail_fast=True)
        )
        elapsed = time.perf_counter() - start

    # Should complete quickly due to fail-fast
    assert elapsed < 1.0, f"Expected < 1.0s with fail-fast, got {elapsed:.3f}s"
    assert result.ok is False

    # Verify shape: only the failed command should be in results (slow was cancelled)
    assert len(result.results) >= 1, "At least the failed command should be in results"
    assert len(result.failures) == 1, "Exactly one failure expected"

    # failures tuple must use remapped indices into the compacted results array
    assert result.failures[0] < len(result.results), (
        f"Failure index {result.failures[0]} out of range for results of "
        f"length {len(result.results)}"
    )

    # first_failure must correctly index the failed result
    assert result.first_failure is not None
    assert result.first_failure is result.results[result.failures[0]]
    assert result.first_failure.exit_code == 99
