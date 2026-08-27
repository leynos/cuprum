"""Concurrent execution of multiple SafeCmd instances.

This module provides helpers for running multiple curated commands concurrently
with optional concurrency limits, while preserving hook semantics and providing
aggregated results.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._concurrent_config import (
    ConcurrentConfig,
    ConcurrentResult,
    _ConcurrentRunConfig,
)
from cuprum.context import current_context
from cuprum.sh import RunOutputOptions

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import CommandResult, SafeCmd


class _FirstFailureError(Exception):
    """Internal exception to signal first failure in fail-fast mode."""

    def __init__(self, index: int, result: CommandResult) -> None:
        """Record the failing command index and its result."""
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
            return await cmd.run(output=config.output, context=config.context)
    return await cmd.run(output=config.output, context=config.context)


def _collect_results_and_failures(
    indexed_results: cabc.Iterable[tuple[int, CommandResult | None]],
) -> tuple[list[CommandResult], list[int], list[int]]:
    """Compact completed results, recording submission and failure indices.

    This is the single canonical "append result, record index if not ok" loop
    shared by the collect-all and fail-fast paths, so the two cannot drift.

    Parameters
    ----------
    indexed_results:
        Pairs of ``(submission_index, result)``. A ``None`` result marks a
        command that was cancelled (fail-fast) and is skipped.

    Returns
    -------
    tuple[list[CommandResult], list[int], list[int]]
        ``(results, submission_indices, failures)`` where ``results`` holds the
        completed commands in encounter order, ``submission_indices`` is the
        original submission index of each entry in ``results``, and
        ``failures`` holds positions within ``results`` for non-zero exits, in
        ascending order.
    """
    results: list[CommandResult] = []
    submission_indices: list[int] = []
    failures: list[int] = []
    for submission_index, result in indexed_results:
        if result is None:
            continue
        position = len(results)
        results.append(result)
        submission_indices.append(submission_index)
        if not result.ok:
            failures.append(position)
    return results, submission_indices, failures


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

    # Re-raise the first BaseException to propagate critical exceptions like
    # CancelledError immediately. This is an intentional trade-off: any
    # subsequent exceptions from other tasks are dropped and not preserved
    # for diagnostics, but it ensures cancellation signals propagate promptly.
    completed: list[CommandResult | None] = []
    for raw in raw_results:
        if isinstance(raw, BaseException):
            raise raw
        completed.append(raw)

    results, submission_indices, failures = _collect_results_and_failures(
        enumerate(completed),
    )
    return ConcurrentResult(
        results=tuple(results),
        failures=tuple(failures),
        submission_indices=tuple(submission_indices),
    )


def _build_final_results(
    results: list[CommandResult | None],
) -> tuple[list[CommandResult], list[int], list[int]]:
    """Build final results, submission indices, and failures from partial results.

    Parameters
    ----------
    results:
        List of CommandResult or None for cancelled commands, indexed by
        original submission position.

    Returns
    -------
    tuple[list[CommandResult], list[int], list[int]]
        ``(final_results, submission_indices, failures)`` where ``final_results``
        contains only completed commands, ``submission_indices`` records each
        completed command's original submission position, and ``failures``
        contains indices into ``final_results`` (not original positions) for
        failed commands, in ascending order.

    post: all(result is not None for result in __return__[0])
    post: __return__[0] == [result for result in results if result is not None]
    post: len(__return__[0]) == sum(1 for result in results if result is not None)
    post: __return__[1] == [i for i, result in enumerate(results) if result is not None]
    post: all(0 <= idx < len(__return__[0]) for idx in __return__[2])
    post: all(not __return__[0][idx].ok for idx in __return__[2])
    post: __return__[2] == sorted(__return__[2])

    """
    return _collect_results_and_failures(enumerate(results))


async def _run_fail_fast(
    commands: cabc.Sequence[SafeCmd],
    semaphore: asyncio.Semaphore | None,
    config: _ConcurrentRunConfig,
) -> ConcurrentResult:
    """Execute commands with fail-fast cancellation on first non-zero exit."""
    results: list[CommandResult | None] = [None] * len(commands)

    async def run_indexed(idx: int, cmd: SafeCmd) -> None:
        """Run one command and raise on the first non-zero exit."""
        result = await _run_with_semaphore(cmd, semaphore, config)
        results[idx] = result
        if not result.ok:
            raise _FirstFailureError(idx, result)

    try:
        async with asyncio.TaskGroup() as tg:
            for idx, cmd in enumerate(commands):
                tg.create_task(run_indexed(idx, cmd))
    except* _FirstFailureError:
        # Expected when a command fails; continue to build result
        pass

    final_results, submission_indices, failures = _build_final_results(results)

    return ConcurrentResult(
        results=tuple(final_results),
        failures=tuple(failures),
        submission_indices=tuple(submission_indices),
    )


async def run_concurrent(
    *commands: SafeCmd,
    config: ConcurrentConfig | None = None,
) -> ConcurrentResult:
    """Execute multiple SafeCmd instances concurrently.

    Parameters
    ----------
    *commands:
        SafeCmd instances to execute concurrently.
    config:
        Configuration for concurrent execution. When None, uses default
        ConcurrentConfig() which allows unlimited concurrency, captures
        output, does not echo, uses current context, and collects all
        results (no fail-fast).

    Returns
    -------
    ConcurrentResult
        Aggregated results in submission order.

    Raises
    ------
    ValueError
        If config.concurrency < 1 or no commands provided.
    ForbiddenProgramError
        If ``current_context().check_allowed()`` rejects a command before
        task creation.
    """  # ruff: ignore[docstring-extraneous-exception] - ForbiddenProgramError propagates from check_allowed
    cfg = config or ConcurrentConfig()

    if not commands:
        msg = "At least one command must be provided"
        raise ValueError(msg)

    # Pre-flight allowlist check using the current CuprumContext (allowlist/hooks)
    # Note: CuprumContext (allowlist) is separate from ExecutionContext (runtime params)
    cuprum_ctx = current_context()
    for cmd in commands:
        cuprum_ctx.check_allowed(cmd.program)

    # Create semaphore if concurrency limit specified
    semaphore = (
        asyncio.Semaphore(cfg.concurrency) if cfg.concurrency is not None else None
    )
    # ExecutionContext is passed through for runtime parameters (env, cwd, etc.)
    run_config = _ConcurrentRunConfig(
        output=RunOutputOptions(capture=cfg.capture, echo=cfg.echo),
        context=cfg.context,
    )

    if cfg.fail_fast:
        return await _run_fail_fast(commands, semaphore, run_config)
    return await _run_collect_all(commands, semaphore, run_config)


def run_concurrent_sync(
    *commands: SafeCmd,
    config: ConcurrentConfig | None = None,
) -> ConcurrentResult:
    """Execute multiple SafeCmd instances concurrently (synchronous wrapper).

    This method mirrors ``run_concurrent()`` by driving the event loop
    internally. All parameters and return semantics are identical.

    Parameters
    ----------
    *commands:
        SafeCmd instances to execute concurrently.
    config:
        Configuration for concurrent execution. When None, uses default
        ConcurrentConfig() which allows unlimited concurrency, captures
        output, does not echo, uses current context, and collects all
        results (no fail-fast).

    Returns
    -------
    ConcurrentResult
        Aggregated results in submission order.

    """
    return asyncio.run(
        run_concurrent(
            *commands,
            config=config,
        ),
    )


__all__ = [
    "ConcurrentConfig",
    "ConcurrentResult",
    "run_concurrent",
    "run_concurrent_sync",
]
