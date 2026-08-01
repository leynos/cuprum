"""Unit tests for normal concurrent execution and concurrency limits.

These cover the happy path of ``run_concurrent``/``run_concurrent_sync``:
result shape and ordering, sync/async parity, the timing behaviour of the
concurrency limit, and argument and allowlist rejection before execution.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time

import pytest

from cuprum import (
    ECHO,
    LS,
    ExecEvent,
    ForbiddenProgramError,
    ScopeConfig,
    observe,
    scoped,
    sh,
)
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
    min_elapsed : float
        Minimum expected elapsed time in seconds. The test will
        fail if execution completes faster than this threshold.
    max_elapsed : float | None
        Maximum expected elapsed time in seconds (optional). If
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

    assert result.ok is True, "all sleep commands should succeed"
    assert len(result.results) == num_commands, (
        "every submitted command must yield a result"
    )
    assert elapsed >= timing.min_elapsed, (
        f"Expected >= {timing.min_elapsed}s with concurrency={concurrency}, "
        f"got {elapsed:.3f}s"
    )
    if timing.max_elapsed is not None:
        assert elapsed < timing.max_elapsed, (
            f"Expected < {timing.max_elapsed}s with concurrency={concurrency}, "
            f"got {elapsed:.3f}s"
        )

def test_repeated_short_lived_captured_commands_complete() -> None:
    """Repeated short-lived captured commands complete without stalling."""
    echo = sh.make(ECHO)
    command = echo("-n", "hello")

    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        for iteration in range(20):
            result = run_concurrent_sync(command)
            command_result = result.results[0]
            assert command_result.ok, (
                "short-lived captured command failed: "
                f"iteration={iteration}, exit_code={command_result.exit_code}"
            )
            assert command_result.stdout == "hello", (
                "captured output length mismatch: "
                f"iteration={iteration}, expected=5, "
                f"actual={len(command_result.stdout or '')}"
            )
class TestConcurrentExecution:
    """Verify concurrent execution results and argument handling."""

    @staticmethod
    def test_run_concurrent_returns_concurrent_result() -> None:
        """run_concurrent returns a ConcurrentResult with results tuple."""
        echo = sh.make(ECHO)
        cmd1 = echo("-n", "one")
        cmd2 = echo("-n", "two")

        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
            result = run_concurrent_sync(cmd1, cmd2)

        assert isinstance(result, ConcurrentResult), (
            "run_concurrent returns a ConcurrentResult"
        )
        assert len(result.results) == 2, "both submitted commands must be reported"
        assert result.ok is True, "both echo commands should succeed"
        assert result.failures == (), "no command failed, so failures must be empty"

    @staticmethod
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

        assert len(result.results) == 3, "all three commands must be reported"
        assert result.results[0].stdout is not None, "stdout is captured by default"
        assert "first" in result.results[0].stdout, "result 0 must be the first command"
        assert result.results[1].stdout is not None, "stdout is captured by default"
        assert "second" in result.results[1].stdout, (
            "result 1 must be the second command"
        )
        assert result.results[2].stdout is not None, "stdout is captured by default"
        assert "third" in result.results[2].stdout, "result 2 must be the third command"

    @staticmethod
    def test_run_concurrent_sync_mirrors_async() -> None:
        """run_concurrent_sync produces identical results to async version."""
        echo = sh.make(ECHO)
        cmd = echo("-n", "hello")

        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
            sync_result = run_concurrent_sync(cmd)
            async_result = asyncio.run(run_concurrent(cmd))

        assert sync_result.ok == async_result.ok, "sync and async must agree on success"
        assert len(sync_result.results) == len(async_result.results), (
            "sync and async must report the same number of results"
        )
        assert sync_result.results[0].stdout == async_result.results[0].stdout, (
            "sync and async must capture identical output"
        )

    @staticmethod
    def test_empty_commands_raises_value_error() -> None:
        """Calling run_concurrent with no commands raises ValueError."""
        with pytest.raises(ValueError, match="At least one command"):
            run_concurrent_sync()

    @staticmethod
    def test_forbidden_program_raises_before_execution() -> None:
        """ForbiddenProgramError is raised before any command executes."""
        echo = sh.make(ECHO)
        cmd1 = echo("-n", "hello")
        cmd2 = echo("-n", "world")
        events: list[ExecEvent] = []

        # Allowlist only LS, so ECHO is forbidden
        forbidden_ctx = scoped(ScopeConfig(allowlist=frozenset([LS])))
        with (
            forbidden_ctx,
            observe(events.append),
            pytest.raises(ForbiddenProgramError),
        ):
            run_concurrent_sync(cmd1, cmd2)

        assert not any(event.phase == "start" for event in events), (
            "allowlist preflight must reject commands before start events are emitted"
        )

    @staticmethod
    def test_single_command_works() -> None:
        """run_concurrent works with a single command."""
        echo = sh.make(ECHO)
        cmd = echo("-n", "solo")

        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
            result = run_concurrent_sync(cmd)

        assert result.ok is True, "the single command should succeed"
        assert len(result.results) == 1, "a single submission yields a single result"
        assert result.results[0].stdout == "solo", "the command output must be captured"

    @staticmethod
    def test_capture_false_returns_none_stdout() -> None:
        """When capture=False, stdout is None in results."""
        echo = sh.make(ECHO)
        cmd = echo("-n", "hello")

        with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
            result = run_concurrent_sync(cmd, config=ConcurrentConfig(capture=False))

        assert result.ok is True, "the command should succeed even without capture"
        assert result.results[0].stdout is None, "capture=False must leave stdout unset"

    @staticmethod
    def test_async_run_concurrent() -> None:
        """run_concurrent works correctly as an async function."""
        echo = sh.make(ECHO)
        cmd1 = echo("-n", "async1")
        cmd2 = echo("-n", "async2")

        async def exercise() -> ConcurrentResult:
            """Run the two echo commands concurrently within an allowlist scope."""
            with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
                return await run_concurrent(cmd1, cmd2)

        result = asyncio.run(exercise())

        assert result.ok is True, "both async commands should succeed"
        assert len(result.results) == 2, "both submitted commands must be reported"


class TestConcurrencyLimits:
    """Verify concurrency limits govern parallel execution."""

    @staticmethod
    @pytest.mark.parametrize(
        ("num_commands", "sleep_seconds", "concurrency", "timing"),
        [
            # Longer sleeps and conservative thresholds avoid flakiness under CI load.
            pytest.param(4, 0.2, 2, _TimingExpectation(min_elapsed=0.3), id="limited"),
            # Four concurrent interpreters each pay their own startup cost, so the
            # bound sits just under the 0.8s sequential runtime (4 * 0.2s) rather
            # than close to the 0.2s ideal: enough margin for spawn and scheduler
            # jitter under load, while still failing if execution serialises.
            pytest.param(
                4,
                0.2,
                None,
                _TimingExpectation(min_elapsed=0.0, max_elapsed=0.78),
                id="unlimited",
            ),
            # Longer sleeps give more reliable sequential timing detection.
            pytest.param(
                3, 0.15, 1, _TimingExpectation(min_elapsed=0.35), id="sequential"
            ),
        ],
    )
    def test_concurrency_limit_governs_parallel_execution(
        num_commands: int,
        sleep_seconds: float,
        concurrency: int | None,
        timing: _TimingExpectation,
    ) -> None:
        """The concurrency limit governs how many commands run in parallel."""
        _assert_concurrent_timing(
            num_commands=num_commands,
            sleep_seconds=sleep_seconds,
            concurrency=concurrency,
            timing=timing,
        )
