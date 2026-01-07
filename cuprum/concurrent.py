"""Concurrent execution of multiple SafeCmd instances.

This module provides helpers for running multiple curated commands concurrently
with optional concurrency limits, while preserving hook semantics and providing
aggregated results.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ

from cuprum.context import current_context

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, ExecutionContext, SafeCmd


@dc.dataclass(frozen=True, slots=True)
class _ConcurrentRunConfig:
    """Internal configuration for concurrent command execution."""

    capture: bool
    echo: bool
    context: ExecutionContext | None


@dc.dataclass(frozen=True, slots=True)
class ConcurrentResult:
    """Aggregated result from concurrent command execution.

    Attributes
    ----------
    results:
        CommandResult instances in submission order (not completion order).
    failures:
        Indices of commands that exited non-zero, in ascending order.

    """

    results: tuple[CommandResult, ...]
    failures: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        """Return True when all commands exited successfully."""
        return len(self.failures) == 0

    @property
    def first_failure(self) -> CommandResult | None:
        """Return the first failed result, or None if all succeeded."""
        if not self.failures:
            return None
        return self.results[self.failures[0]]


class _FirstFailureError(Exception):
    """Internal exception to signal first failure in fail-fast mode."""

    def __init__(self, index: int, result: CommandResult) -> None:
        super().__init__(f"Command at index {index} failed")
        self.index = index
        self.result = result


async def _run_with_semaphore(
    cmd: SafeCmd,
    semaphore: asyncio.Semaphore | None,
    config: _ConcurrentRunConfig,
) -> CommandResult:
    """Execute a single command, optionally throttled by semaphore."""
    if semaphore is not None:
        async with semaphore:
            return await cmd.run(
                capture=config.capture, echo=config.echo, context=config.context
            )
    return await cmd.run(
        capture=config.capture, echo=config.echo, context=config.context
    )


async def _run_collect_all(
    commands: cabc.Sequence[SafeCmd],
    semaphore: asyncio.Semaphore | None,
    config: _ConcurrentRunConfig,
) -> ConcurrentResult:
    """Execute commands concurrently, collecting all results."""
    tasks = [
        asyncio.create_task(_run_with_semaphore(cmd, semaphore, config))
        for cmd in commands
    ]

    # Gather results, capturing exceptions
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    # Process results and identify failures
    results: list[CommandResult] = []
    failures: list[int] = []

    for idx, raw in enumerate(raw_results):
        if isinstance(raw, BaseException):
            # Re-raise unexpected exceptions (e.g., CancelledError)
            raise raw
        results.append(raw)
        if not raw.ok:
            failures.append(idx)

    return ConcurrentResult(
        results=tuple(results),
        failures=tuple(failures),
    )


async def _run_fail_fast(
    commands: cabc.Sequence[SafeCmd],
    semaphore: asyncio.Semaphore | None,
    config: _ConcurrentRunConfig,
) -> ConcurrentResult:
    """Execute commands with fail-fast cancellation on first non-zero exit."""
    results: list[CommandResult | None] = [None] * len(commands)
    failures: list[int] = []
    first_failure_index: int | None = None

    async def run_indexed(idx: int, cmd: SafeCmd) -> None:
        nonlocal first_failure_index
        result = await _run_with_semaphore(cmd, semaphore, config)
        results[idx] = result
        if not result.ok and first_failure_index is None:
            first_failure_index = idx
            raise _FirstFailureError(idx, result)

    try:
        async with asyncio.TaskGroup() as tg:
            for idx, cmd in enumerate(commands):
                tg.create_task(run_indexed(idx, cmd))
    except* _FirstFailureError:
        # Expected when a command fails; continue to build result
        pass

    # Build final results from whatever completed
    final_results: list[CommandResult] = []
    for idx, result in enumerate(results):
        if result is not None:
            final_results.append(result)
            if not result.ok:
                failures.append(idx)
        else:
            # Command was cancelled before completion; not included in results
            pass

    # Sort failures for consistent ordering
    failures.sort()

    return ConcurrentResult(
        results=tuple(final_results),
        failures=tuple(failures),
    )


async def run_concurrent(  # noqa: PLR0913
    *commands: SafeCmd,
    concurrency: int | None = None,
    capture: bool = True,
    echo: bool = False,
    context: ExecutionContext | None = None,
    fail_fast: bool = False,
) -> ConcurrentResult:
    """Execute multiple SafeCmd instances concurrently.

    Parameters
    ----------
    *commands:
        SafeCmd instances to execute concurrently.
    concurrency:
        Maximum concurrent executions. None means unlimited.
        Must be >= 1 when provided.
    capture:
        When True, capture stdout/stderr for each command.
    echo:
        When True, tee output to configured sinks.
    context:
        Shared execution context for all commands.
    fail_fast:
        When True, cancel remaining commands after first failure.

    Returns
    -------
    ConcurrentResult
        Aggregated results in submission order.

    Raises
    ------
    ForbiddenProgramError
        If any command's program is not in the current allowlist.
    ValueError
        If concurrency < 1 or no commands provided.

    """
    if not commands:
        msg = "At least one command must be provided"
        raise ValueError(msg)

    if concurrency is not None and concurrency < 1:
        msg = f"concurrency must be >= 1, got {concurrency}"
        raise ValueError(msg)

    # Pre-flight allowlist check for all commands
    ctx = current_context()
    for cmd in commands:
        ctx.check_allowed(cmd.program)

    # Create semaphore if concurrency limit specified
    semaphore = asyncio.Semaphore(concurrency) if concurrency is not None else None
    config = _ConcurrentRunConfig(capture=capture, echo=echo, context=context)

    if fail_fast:
        return await _run_fail_fast(commands, semaphore, config)
    return await _run_collect_all(commands, semaphore, config)


def run_concurrent_sync(  # noqa: PLR0913
    *commands: SafeCmd,
    concurrency: int | None = None,
    capture: bool = True,
    echo: bool = False,
    context: ExecutionContext | None = None,
    fail_fast: bool = False,
) -> ConcurrentResult:
    """Execute multiple SafeCmd instances concurrently (synchronous wrapper).

    This method mirrors ``run_concurrent()`` by driving the event loop
    internally. All parameters and return semantics are identical.

    Parameters
    ----------
    *commands:
        SafeCmd instances to execute concurrently.
    concurrency:
        Maximum concurrent executions. None means unlimited.
        Must be >= 1 when provided.
    capture:
        When True, capture stdout/stderr for each command.
    echo:
        When True, tee output to configured sinks.
    context:
        Shared execution context for all commands.
    fail_fast:
        When True, cancel remaining commands after first failure.

    Returns
    -------
    ConcurrentResult
        Aggregated results in submission order.

    """
    return asyncio.run(
        run_concurrent(
            *commands,
            concurrency=concurrency,
            capture=capture,
            echo=echo,
            context=context,
            fail_fast=fail_fast,
        ),
    )


__all__ = [
    "ConcurrentResult",
    "run_concurrent",
    "run_concurrent_sync",
]
