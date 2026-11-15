"""A specification oracle for observe-hook dispatch tests.

The oracle states what ``_emit_exec_event`` must do for any sequence of hook
shapes, independently of the dispatcher's own decision. It calls a *fresh* hook
of each shape and asks :func:`inspect.isawaitable` about the result, so a change
that narrowed the public contract would fail a comparison rather than silently
redefine it. It also states the log records the dispatcher must emit, so a
comparison covers each full record, not just a message prefix.

The recorded messages are a specification, not a copy of the implementation:
they are what the released behaviour promises, and a property run fails if the
dispatcher's wording or structured fields drift from them.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import inspect
import logging
import types
import typing as typ

from cuprum.unittests._observe_hook_dispatch_shapes import TASK_FAILURE_KINDS
from cuprum.unittests._observe_hook_dispatch_support import make_probe

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent

_LOGGER_NAME = "cuprum._observability"

# Message prefixes the dispatcher owns. A comparison considers only records
# that carry one of these, so unrelated logging cannot make it pass or fail.
DISPATCH_MESSAGE_PREFIXES: tuple[str, ...] = (
    "observe_hook_task_scheduled",
    "observe_hook_failed",
    "observe_hook_task_started",
    "observe_hook_task_finished",
    "observe_hook_task_failed",
)

# The records emitted from inside a scheduled task. They are compared without
# regard to order because a suspension inside an awaitable interleaves them.
_LIFECYCLE_PREFIXES: tuple[str, ...] = (
    "observe_hook_task_started",
    "observe_hook_task_finished",
    "observe_hook_task_failed",
)


@dc.dataclass(frozen=True, slots=True)
class Failure:
    """One raising hook, at its position in the call order.

    Attributes
    ----------
    index:
        The position in ``DispatchExpectation.called`` of the raising hook.
    error:
        The exception the hook raised.

    """

    index: int
    error: BaseException


@dc.dataclass(frozen=True, slots=True)
class DispatchExpectation:
    """What the dispatcher must do for one sequence of hook kinds.

    Attributes
    ----------
    called:
        Kinds the dispatcher must call, in order.
    scheduled:
        Kinds whose result is awaitable, among the called non-raising hooks.
    failures:
        Every raising hook, in call order. A non-settled dispatch stops at the
        first; a settled dispatch calls and reports each one, while surfacing
        only the first.
    check_error:
        The exception ``inspect.isawaitable`` raised, or ``None``. It escapes
        the dispatcher unwrapped, so it takes precedence over the failures.
    check_index:
        The position in ``called`` of the hook whose awaitable check raised, or
        ``None``.

    """

    called: tuple[str, ...]
    scheduled: tuple[str, ...]
    failures: tuple[Failure, ...]
    check_error: BaseException | None
    check_index: int | None

    @property
    def error(self) -> BaseException | None:
        """The failure the dispatcher must surface, if any."""
        return self.failures[0].error if self.failures else None

    @property
    def error_index(self) -> int | None:
        """The call position of the surfaced failure, if any."""
        return self.failures[0].index if self.failures else None


@dc.dataclass(frozen=True, slots=True)
class ExpectedRecord:
    """One log record the dispatcher must emit.

    Attributes
    ----------
    level:
        The record's numeric level.
    message:
        The fully formatted message, as ``LogRecord.getMessage()`` returns it.
    extra:
        The structured fields the record must carry, with their values.
    exc_type:
        The type the record's ``exc_info`` must name, or ``None`` for no
        exception information.

    """

    level: int
    message: str
    extra: dict[str, object]
    exc_type: type[BaseException] | None


def _close_result(result: object) -> None:
    """Close a coroutine or generator the oracle produced but will not await.

    The exact-type test is deliberate: ``inspect.iscoroutine`` consults
    ``__class__``, which the ``check_raises`` shape makes raise, and closing a
    result must not mask the failure the oracle is about to report.
    """
    if type(result) in {types.CoroutineType, types.GeneratorType}:
        typ.cast("cabc.Coroutine[object, object, object]", result).close()


def _probe_outcome(
    kind: str,
    event: ExecEvent,
) -> tuple[str, bool, BaseException | None]:
    """Call a fresh hook of ``kind`` and classify what it did.

    Returns
    -------
    tuple[str, bool, BaseException | None]
        ``"raise"`` with the hook's error, ``"check"`` with the awaitable
        check's error, or ``"ok"`` with whether the result is awaitable.
    """
    probe = make_probe(kind)
    try:
        result = probe.hook(event)
    # Any failure is part of the specification, including the non-``Exception``
    # shapes the dispatcher must still wrap.
    except BaseException as exc:  # ruff: ignore[blind-except]
        return "raise", False, exc
    try:
        is_awaitable = inspect.isawaitable(result)
    # The ``check_raises`` shape makes the check itself raise; that escape is
    # part of the specification too.
    except BaseException as exc:  # ruff: ignore[blind-except]
        return "check", False, exc
    finally:
        _close_result(result)
    return "ok", is_awaitable, None


@dc.dataclass(slots=True)
class _Walk:
    """The hooks an oracle walk has called, scheduled, and seen fail.

    Attributes
    ----------
    settled:
        Whether dispatch keeps calling hooks after a synchronous failure.
    called:
        The kinds of the hooks the walk has visited, in call order.
    scheduled:
        The kinds whose results the dispatcher must schedule.
    failures:
        The synchronous failures the dispatcher must report.
    """

    settled: bool
    called: list[str] = dc.field(default_factory=list)
    scheduled: list[str] = dc.field(default_factory=list)
    failures: list[Failure] = dc.field(default_factory=list)

    def run(
        self,
        kinds: cabc.Sequence[str],
        event: ExecEvent,
    ) -> DispatchExpectation:
        """Visit each hook kind in turn, stopping where dispatch must stop."""
        for index, kind in enumerate(kinds):
            halted = self._visit(index, kind, event)
            if halted is not None:
                return halted
        return self.freeze()

    def freeze(
        self,
        *,
        check_error: BaseException | None = None,
        check_index: int | None = None,
    ) -> DispatchExpectation:
        """Freeze the walk so far into the expectation it implies."""
        return DispatchExpectation(
            called=tuple(self.called),
            scheduled=tuple(self.scheduled),
            failures=tuple(self.failures),
            check_error=check_error,
            check_index=check_index,
        )

    def _visit(
        self,
        index: int,
        kind: str,
        event: ExecEvent,
    ) -> DispatchExpectation | None:
        """Visit one hook, returning the expectation if the walk must halt."""
        self.called.append(kind)
        outcome, is_awaitable, error = _probe_outcome(kind, event)
        if outcome == "check":
            return self.freeze(check_error=error, check_index=index)
        if outcome == "raise":
            return self._halt_on_failure(index, error)
        if is_awaitable:
            self.scheduled.append(kind)
        return None

    def _halt_on_failure(
        self,
        index: int,
        error: BaseException | None,
    ) -> DispatchExpectation | None:
        """Record a synchronous failure, halting unless the walk is settled."""
        self.failures.append(
            Failure(index=index, error=typ.cast("BaseException", error))
        )
        return None if self.settled else self.freeze()


def expected_dispatch(
    kinds: cabc.Sequence[str],
    event: ExecEvent,
    *,
    settled: bool = False,
) -> DispatchExpectation:
    """State what the dispatcher must do for ``kinds`` and ``event``.

    With ``settled=False`` dispatch stops at the first raising hook. With
    ``settled=True`` it calls every hook, reports each synchronous failure, and
    surfaces the first one afterwards, except that a failing awaitable check
    escapes at once and prevents the hooks after it from running.

    Returns
    -------
    DispatchExpectation
        The hooks that must be called, the results that must be scheduled, the
        failures that must be reported, and the failure the sequence must
        surface or escape with, if any.
    """
    return _Walk(settled=settled).run(kinds, event)


def scheduled_probe_indices(
    expectation: DispatchExpectation,
) -> tuple[int, ...]:
    """Return the positions in ``expectation.called`` whose results schedule.

    Each hook kind is deterministic: every occurrence of a kind produces the
    same awaitability, so matching the ordered ``scheduled`` projection against
    ``called`` by kind identifies the exact hooks that scheduled a task.

    Returns
    -------
    tuple[int, ...]
        The positions in ``called`` whose results must be scheduled, in order.
    """
    indices: list[int] = []
    cursor = 0
    for index, kind in enumerate(expectation.called):
        if (
            cursor < len(expectation.scheduled)
            and expectation.scheduled[cursor] == kind
        ):
            indices.append(index)
            cursor += 1
    return tuple(indices)


def _scheduling_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> list[ExpectedRecord]:
    """State the scheduling and failure records in the order hooks run.

    Each record is emitted where its hook sits in the call order, not after the
    others: a settled dispatch keeps calling hooks past a failure, so a failure
    record can precede later scheduling records. Its task count is the number
    scheduled *so far*, which is what the dispatcher has to report. Cancellation
    is not a failure and is deliberately not reported.

    Returns
    -------
    list[ExpectedRecord]
        The scheduling and failure records, in emission order.
    """
    phase = event.phase
    program = str(event.program)
    scheduled_at = set(scheduled_probe_indices(expectation))
    failures_at = {
        failure.index: failure.error
        for failure in expectation.failures
        if not issubclass(type(failure.error), asyncio.CancelledError)
    }
    records: list[ExpectedRecord] = []
    count = 0
    for index in range(len(expectation.called)):
        if index in scheduled_at:
            count += 1
            records.append(
                ExpectedRecord(
                    level=logging.DEBUG,
                    message=(
                        f"observe_hook_task_scheduled phase={phase} "
                        f"program={program} count={count}"
                    ),
                    extra={
                        "cuprum_phase": phase,
                        "cuprum_program": program,
                        "cuprum_scheduled_task_count": count,
                    },
                    exc_type=None,
                )
            )
        error = failures_at.get(index)
        if error is not None:
            error_type = type(error).__name__
            records.append(
                ExpectedRecord(
                    level=logging.WARNING,
                    message=(
                        f"observe_hook_failed phase={phase} "
                        f"program={program} error={error_type}"
                    ),
                    extra={
                        "cuprum_phase": phase,
                        "cuprum_program": program,
                        "cuprum_error_type": error_type,
                        "cuprum_scheduled_task_count": count,
                    },
                    exc_type=type(error),
                )
            )
    return records


def _lifecycle_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> list[ExpectedRecord]:
    """State each scheduled task's lifecycle, including any awaited failure."""
    records: list[ExpectedRecord] = []
    for kind in expectation.scheduled:
        records.append(
            ExpectedRecord(
                level=logging.DEBUG,
                message=f"observe_hook_task_started phase={event.phase}",
                extra={"cuprum_phase": event.phase},
                exc_type=None,
            )
        )
        failure_type = TASK_FAILURE_KINDS.get(kind)
        if failure_type is not None:
            # ``Logger.exception`` reports at ERROR, not WARNING.
            records.append(
                ExpectedRecord(
                    level=logging.ERROR,
                    message=(
                        f"observe_hook_task_failed phase={event.phase} "
                        f"error={failure_type.__name__}"
                    ),
                    extra={
                        "cuprum_phase": event.phase,
                        "cuprum_error_type": failure_type.__name__,
                    },
                    exc_type=failure_type,
                )
            )
            continue
        records.append(
            ExpectedRecord(
                level=logging.DEBUG,
                message=f"observe_hook_task_finished phase={event.phase}",
                extra={"cuprum_phase": event.phase},
                exc_type=None,
            )
        )
    return records


def expected_records(
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> tuple[list[ExpectedRecord], list[ExpectedRecord]]:
    """State the log records the dispatcher must emit for ``expectation``.

    Returns
    -------
    tuple[list[ExpectedRecord], list[ExpectedRecord]]
        The scheduling and failure records in emission order, then the task
        lifecycle records, which are compared without regard to order because
        a suspension inside an awaitable may interleave them.
    """
    return (
        _scheduling_records(expectation, event),
        _lifecycle_records(expectation, event),
    )
