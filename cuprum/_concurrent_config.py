"""Configuration and result types for concurrent command execution.

This module holds the dataclasses that describe how concurrent execution is
configured and the aggregated results it produces. It is an implementation
detail of :mod:`cuprum.concurrent`; import the public names from there.
"""

from __future__ import annotations

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions


@dc.dataclass(frozen=True, slots=True)
class _ConcurrentRunConfig:
    """Internal configuration for concurrent command execution."""

    output: RunOutputOptions
    context: ExecutionContext | None


@dc.dataclass(frozen=True, slots=True)
class ConcurrentConfig:
    """Configuration for concurrent command execution.

    Attributes
    ----------
    concurrency:
        Maximum number of commands to execute concurrently. When None,
        all commands are started immediately with no throttling.
        Must be >= 1 when provided. A non-integer or ``bool`` value raises
        :class:`TypeError`; a value below 1 raises :class:`ValueError`.
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
        if self.concurrency is not None:
            # ``type(...) is not int`` rejects ``bool`` (an ``int`` subclass)
            # so ``True``/``False`` are not treated as valid concurrency.
            if type(self.concurrency) is not int:
                msg = (
                    f"concurrency must be an int, got {type(self.concurrency).__name__}"
                )
                raise TypeError(msg)
            if self.concurrency < 1:
                msg = f"concurrency must be >= 1, got {self.concurrency}"
                raise ValueError(msg)


def _validate_index_type(index: int, *, type_error_message: str) -> None:
    """Validate that an index is an exact integer."""
    # ``type(...) is not int`` rejects ``bool`` (an ``int`` subclass) so
    # ``True``/``False`` are not treated as valid indexes.
    if type(index) is not int:
        msg = f"{type_error_message}, got {type(index).__name__}"
        raise TypeError(msg)


def _validate_index_bounds(
    index: int,
    *,
    subject: str,
    upper_bound: int | None,
) -> None:
    """Validate an index against its optional exclusive upper bound."""
    if upper_bound is None:
        if index < 0:
            msg = f"{subject} must be non-negative, got {index}"
            raise ValueError(msg)
        return
    if not 0 <= index < upper_bound:
        msg = f"{subject} index {index} is out of range for {upper_bound} results"
        raise ValueError(msg)


def _validate_index_sequence(
    indices: tuple[int, ...],
    *,
    subject: str,
    type_error_message: str,
    upper_bound: int | None,
) -> None:
    """Validate exact-int, in-bounds, strictly ascending index sequences.

    ``upper_bound`` is the exclusive ceiling each index must fall below, or
    ``None`` when only non-negativity is required. The two callers differ in
    exactly that respect, so it is the sole behavioural parameter; ``subject``
    and ``type_error_message`` keep each call site's diagnostic wording.

    Raises
    ------
    TypeError
        If an index is not an exact ``int`` (``bool`` is rejected).
    ValueError
        If an index is negative, exceeds ``upper_bound``, or the sequence is
        not strictly ascending.
    """
    previous = -1
    for index in indices:
        _validate_index_type(index, type_error_message=type_error_message)
        _validate_index_bounds(index, subject=subject, upper_bound=upper_bound)
        if index <= previous:
            msg = f"{subject} must be strictly ascending and unique, got {indices}"
            raise ValueError(msg)
        previous = index


def _validate_failure_indices(
    failures: tuple[int, ...],
    *,
    result_count: int,
) -> None:
    """Validate that failure indices are in range and strictly ascending."""
    _validate_index_sequence(
        failures,
        subject="failures",
        type_error_message="failures index must be an int",
        upper_bound=result_count,
    )


def _validate_submission_index_values(submission_indices: tuple[int, ...]) -> None:
    """Validate submission index types, bounds, and ordering.

    Submission indices are *original* command positions, so unlike ``failures``
    they are deliberately NOT bounded by the compacted result count: fail-fast
    compaction drops cancelled commands, leaving survivors whose submission
    index can exceed that length (one survivor submitted third is ``(2,)``).
    They must still be non-negative ints in strictly ascending order, which is
    what every producer builds by enumerating in submission order.

    Raises
    ------
    TypeError
        If an index is not an exact ``int`` (``bool`` is rejected).
    ValueError
        If an index is negative, or the sequence is not strictly ascending.
    """  # noqa: DOC502 - both propagate from the shared index validator
    _validate_index_sequence(
        submission_indices,
        subject="submission_indices",
        type_error_message="submission_indices must be ints",
        upper_bound=None,
    )


def _resolve_submission_indices(
    submission_indices: tuple[int, ...] | None,
    *,
    result_count: int,
) -> tuple[int, ...]:
    """Backfill omitted submission indices or validate a supplied sequence."""
    if submission_indices is None:
        # ``None`` means "omitted": backfill the identity sequence (even for
        # empty ``results``) so the mapping stays parallel to ``results``.
        return tuple(range(result_count))
    # Validate a supplied sequence eagerly so a length mismatch fails fast here
    # rather than as a later ``IndexError``.
    if len(submission_indices) != result_count:
        msg = (
            f"submission_indices length ({len(submission_indices)}) "
            f"must match results length ({result_count})"
        )
        raise ValueError(msg)
    _validate_submission_index_values(submission_indices)
    return submission_indices


@dc.dataclass(frozen=True, slots=True, init=False)
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
        command they submitted, uniformly across both execution modes. Pass
        ``None`` (the default) to backfill the identity sequence (even for
        empty ``results``); any supplied sequence — including an empty tuple —
        whose length differs from ``results`` raises :class:`ValueError`.

    """

    results: tuple[CommandResult, ...]
    failures: tuple[int, ...] = ()
    submission_indices: tuple[int, ...] = dc.field(init=False)

    def __init__(
        self,
        results: tuple[CommandResult, ...],
        failures: tuple[int, ...] = (),
        submission_indices: tuple[int, ...] | None = None,
    ) -> None:
        """Initialize a concurrent result and resolve its submission mapping."""
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "failures", failures)
        self.__post_init__(submission_indices)

    def __post_init__(
        self,
        submission_indices: tuple[int, ...] | None,
    ) -> None:
        """Validate result indexes and the submission mapping."""
        result_count = len(self.results)
        _validate_failure_indices(self.failures, result_count=result_count)
        resolved_submission_indices = _resolve_submission_indices(
            submission_indices, result_count=result_count
        )
        object.__setattr__(self, "submission_indices", resolved_submission_indices)

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
