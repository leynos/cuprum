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
class ConcurrentConfig:
    """Configuration for concurrent command execution.

    Attributes
    ----------
    concurrency:
        Maximum number of commands to execute concurrently. When None,
        all commands are started immediately with no throttling.
        Must be >= 1 when provided.
    capture:
        When True, capture stdout/stderr for each command into the
        CommandResult. When False, output is not captured and stdout
        will be None in results.
    echo:
        When True, tee output to configured sinks (stdout/stderr by
        default) in addition to capturing.
    context:
        Shared execution context for all commands. When None, uses
        the current context from the execution environment.
    fail_fast:
        When True, cancel remaining commands after the first command
        exits with a non-zero status. When False (default), all
        commands run to completion regardless of individual failures.

    """

    concurrency: int | None = None
    capture: bool = True
    echo: bool = False
    context: ExecutionContext | None = None
    fail_fast: bool = False

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        if self.concurrency is not None and self.concurrency < 1:
            msg = f"concurrency must be >= 1, got {self.concurrency}"
            raise ValueError(msg)


@dc.dataclass(frozen=True, slots=True)
class ConcurrentResult:
    """Aggregated result from concurrent command execution.

    Attributes
    ----------
    results:
        CommandResult instances in submission order (not completion order).
        When ``fail_fast=True`` is used, execution may cancel remaining
        commands after the first failure; in this case, results contains
        only the completed (non-cancelled) CommandResult instances and
        may be a subset of the originally submitted commands.
    failures:
        Indices into ``results`` for commands that exited non-zero, in
        ascending order. Note: these are indices into the ``results``
        tuple, not the original submission indices.

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

    async def execute() -> CommandResult:
        return await cmd.run(
            capture=config.capture, echo=config.echo, context=config.context
        )

    if semaphore is not None:
        async with semaphore:
            return await execute()
    return await execute()


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

    # Re-raise the first BaseException to propagate critical exceptions like
    # CancelledError immediately. This is an intentional trade-off: any
    # subsequent exceptions from other tasks are dropped and not preserved
    # for diagnostics, but it ensures cancellation signals propagate promptly.
    for idx, raw in enumerate(raw_results):
        if isinstance(raw, BaseException):
            raise raw
        results.append(raw)
        if not raw.ok:
            failures.append(idx)

    return ConcurrentResult(
        results=tuple(results),
        failures=tuple(failures),
    )


def _build_final_results(
    results: list[CommandResult | None],
) -> tuple[list[CommandResult], list[int]]:
    """Build final results and remapped failure indices from partial results.

    Parameters
    ----------
    results:
        List of CommandResult or None for cancelled commands.

    Returns
    -------
    tuple[list[CommandResult], list[int]]
        Tuple of (final_results, remapped_failures) where final_results contains
        only completed commands and remapped_failures contains indices into
        final_results (not original positions) for failed commands, in ascending
        order.

    """
    final_results: list[CommandResult] = []
    failures: list[int] = []

    for result in results:
        if result is not None:
            compacted_idx = len(final_results)
            final_results.append(result)
            if not result.ok:
                failures.append(compacted_idx)

    # failures are already in ascending order since we process sequentially
    return final_results, failures


async def _run_fail_fast(
    commands: cabc.Sequence[SafeCmd],
    semaphore: asyncio.Semaphore | None,
    config: _ConcurrentRunConfig,
) -> ConcurrentResult:
    """Execute commands with fail-fast cancellation on first non-zero exit."""
    results: list[CommandResult | None] = [None] * len(commands)

    async def run_indexed(idx: int, cmd: SafeCmd) -> None:
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

    final_results, failures = _build_final_results(results)

    return ConcurrentResult(
        results=tuple(final_results),
        failures=tuple(failures),
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
    ForbiddenProgramError
        If any command's program is not in the current allowlist.
    ValueError
        If config.concurrency < 1 or no commands provided.

    """
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
        capture=cfg.capture, echo=cfg.echo, context=cfg.context
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
