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
from cuprum.sh import RunOutputOptions

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
        ascending order. These are positions within the (possibly compacted)
        ``results`` tuple, **not** original submission positions; in fail-fast
        mode ``results`` omits cancelled commands, so a ``failures`` index does
        not equal the submitted command's position. Use
        :attr:`failure_submission_indices` for the submission-stable mapping.
    submission_indices:
        Original submission index of each entry in ``results``, parallel to it.
        In collect-all mode this is the identity ``(0, 1, …, n-1)``; in
        fail-fast mode it lists the submission positions of the completed
        commands. Lets callers map any result — or failure — back to the
        command they submitted, uniformly across both execution modes. Defaults
        to the identity sequence when not supplied.

    """

    results: tuple[CommandResult, ...]
    failures: tuple[int, ...] = ()
    submission_indices: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        """Populate ``submission_indices`` with the identity when unset."""
        if not self.submission_indices and self.results:
            object.__setattr__(
                self,
                "submission_indices",
                tuple(range(len(self.results))),
            )

    @property
    def ok(self) -> bool:
        """Return True when all commands exited successfully."""
        return not self.failures

    @property
    def first_failure(self) -> CommandResult | None:
        """Return the first failed result, or None if all succeeded."""
        if not self.failures:
            return None
        return self.results[self.failures[0]]

    @property
    def failure_submission_indices(self) -> tuple[int, ...]:
        """Original submission indices of failed commands, ascending.

        Unlike :attr:`failures` (positions within the compacted ``results``),
        these are stable across both collect-all and fail-fast modes: each
        value is the position at which the failing command was submitted.
        """
        return tuple(self.submission_indices[index] for index in self.failures)


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
    output = RunOutputOptions(capture=config.capture, echo=config.echo)
    if semaphore is not None:
        async with semaphore:
            return await cmd.run(output=output, context=config.context)
    return await cmd.run(output=output, context=config.context)


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
    post: all(0 <= idx < len(__return__[0]) for idx in __return__[1])
    post: all(not __return__[0][idx].ok for idx in __return__[1])
    post: __return__[1] == sorted(__return__[1])

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
