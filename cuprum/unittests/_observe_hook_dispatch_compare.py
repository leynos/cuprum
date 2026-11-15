"""Compare observe-hook dispatch against the specification oracle.

These helpers run the real dispatcher over a sequence of hooks and report the
first way its behaviour departs from :mod:`_observe_hook_dispatch_oracle`. The
comparison covers hook calls, the surfaced failure, the scheduled tasks — count,
order, name, wrapper and wrapped result — and every dispatch log record with its
exact message and structured fields.

The task view is captured *before* the scheduled tasks are awaited: a completed
coroutine drops its frame, so the wrapper identity and the wrapped result are
only observable at scheduling time.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import typing as typ
from itertools import starmap

from cuprum import ECHO
from cuprum._observability import _await_awaitable, _ExecEventEmissionError
from cuprum.events import ExecEvent
from cuprum.unittests._observe_hook_dispatch_oracle import (
    _LIFECYCLE_PREFIXES,
    _LOGGER_NAME,
    DISPATCH_MESSAGE_PREFIXES,
    DispatchExpectation,
    ExpectedRecord,
    expected_dispatch,
    expected_records,
    scheduled_probe_indices,
)
from cuprum.unittests._observe_hook_dispatch_support import HookProbe, make_probe

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging

    from cuprum.events import ExecHook, ExecPhase

type _Dispatch = cabc.Callable[
    [tuple[ExecHook, ...], ExecEvent],
    list[asyncio.Task[None]],
]

# The structured fields every dispatch record may carry. Comparing them by this
# fixed list lets lifecycle records be sorted independently of their order.
_RECORD_FIELDS: tuple[str, ...] = (
    "cuprum_phase",
    "cuprum_program",
    "cuprum_error_type",
    "cuprum_scheduled_task_count",
)


@dc.dataclass(frozen=True, slots=True)
class _Outcome:
    """The dispatcher's observable outcome, normalized for comparison."""

    error: BaseException | None
    wrapped: bool
    tasks: tuple[asyncio.Task[None], ...]


@dc.dataclass(frozen=True, slots=True)
class _TaskView:
    """A scheduled task as it looked when the dispatcher returned it."""

    name: str
    wrapper_code: object
    wrapped: object


def make_event(phase: str) -> ExecEvent:
    """Return a minimal execution event for ``phase``."""
    return ExecEvent(
        phase=typ.cast("ExecPhase", phase),
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={},
    )


def _run_dispatch(
    dispatch: _Dispatch,
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> _Outcome:
    """Run ``dispatch`` and normalize its result or failure."""
    try:
        scheduled = dispatch(hooks, event)
    except _ExecEventEmissionError as exc:
        return _Outcome(error=exc.error, wrapped=True, tasks=tuple(exc.scheduled_tasks))
    # The check failure and non-``Exception`` shapes escape unwrapped by
    # design, so every remaining failure is part of what must be compared.
    except BaseException as exc:  # ruff: ignore[blind-except]
        return _Outcome(error=exc, wrapped=False, tasks=())
    return _Outcome(error=None, wrapped=False, tasks=tuple(scheduled))


def _task_view(task: asyncio.Task[None]) -> _TaskView:
    """Capture a task's wrapper and wrapped result before the task runs."""
    coro = task.get_coro()
    frame = getattr(coro, "cr_frame", None)
    wrapped = None if frame is None else frame.f_locals.get("awaitable")
    return _TaskView(
        name=task.get_name(),
        wrapper_code=getattr(coro, "cr_code", None),
        wrapped=wrapped,
    )


async def _settle(tasks: cabc.Sequence[asyncio.Task[None]]) -> None:
    """Await every scheduled task so no pending task outlives the example."""
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _call_mismatch(
    expectation: DispatchExpectation,
    probes: cabc.Sequence[HookProbe],
) -> str | None:
    """Check that each hook was called exactly when the specification says."""
    for index, probe in enumerate(probes):
        expected = 1 if index < len(expectation.called) else 0
        if len(probe.calls) != expected:
            return (
                f"{probe.kind} was called {len(probe.calls)} times, expected {expected}"
            )
    return None


def _check_error_mismatch(
    expectation: DispatchExpectation,
    outcome: _Outcome,
) -> str | None:
    """Check that a check failure escapes the dispatcher unwrapped."""
    if outcome.wrapped:
        return "a check failure must escape unwrapped"
    if type(outcome.error) is not type(expectation.check_error):
        return (
            f"check failure {type(outcome.error).__name__} != "
            f"{type(expectation.check_error).__name__}"
        )
    return None


def _hook_error_mismatch(
    expectation: DispatchExpectation,
    probes: cabc.Sequence[HookProbe],
    outcome: _Outcome,
) -> str | None:
    """Check that a raising hook is wrapped with its original exception."""
    if expectation.error is None:
        if outcome.error is not None:
            return f"unexpected error {type(outcome.error).__name__}"
        return None
    if not outcome.wrapped:
        return "a raising hook must be wrapped in _ExecEventEmissionError"
    index = typ.cast("int", expectation.error_index)
    failing = probes[index]
    if outcome.error is not failing.raised[0]:
        return "the wrapped error must be the original exception object"
    return None


def _error_mismatch(
    expectation: DispatchExpectation,
    probes: cabc.Sequence[HookProbe],
    outcome: _Outcome,
) -> str | None:
    """Check the failure shape against the specification."""
    if expectation.check_error is not None:
        return _check_error_mismatch(expectation, outcome)
    return _hook_error_mismatch(expectation, probes, outcome)


def _task_mismatch(
    expectation: DispatchExpectation,
    probes: cabc.Sequence[HookProbe],
    views: cabc.Sequence[_TaskView],
    event: ExecEvent,
) -> str | None:
    """Check task count, order, name, wrapper, and wrapped result."""
    if len(views) != len(expectation.scheduled):
        return f"scheduled {len(views)} tasks, expected {len(expectation.scheduled)}"
    expected_name = f"cuprum.observe.{event.phase}"
    indices = scheduled_probe_indices(expectation)
    for position, (view, probe_index) in enumerate(zip(views, indices, strict=True)):
        if view.name != expected_name:
            return f"task named {view.name!r}, expected {expected_name!r}"
        if view.wrapper_code is not _await_awaitable.__code__:
            return f"task {position} must wrap _await_awaitable"
        expected_result = probes[probe_index].results[0]
        if view.wrapped is not expected_result:
            return (
                f"task {position} wraps a result from {probes[probe_index].kind}, "
                "not the result of the hook that scheduled it"
            )
    return None


def _extra_mismatch(
    label: str,
    record: logging.LogRecord,
    expected: cabc.Mapping[str, object],
) -> str | None:
    """Return the first structured field of ``record`` that ``expected`` rejects."""
    for key, value in expected.items():
        actual = getattr(record, key, None)
        if actual != value:
            return f"{label} {key}={actual!r}, expected {value!r}"
    return None


def _first_mismatch(mismatches: cabc.Iterable[str | None]) -> str | None:
    """Return the first real mismatch, if any."""
    return next((mismatch for mismatch in mismatches if mismatch is not None), None)


def _record_mismatch(
    record: logging.LogRecord,
    expected: ExpectedRecord,
) -> str | None:
    """Check one record's full message, level, extras, and exception type."""
    if record.name != _LOGGER_NAME:
        return f"record logger {record.name!r}, expected {_LOGGER_NAME!r}"
    if record.levelno != expected.level:
        return f"record level {record.levelno}, expected {expected.level}"
    message = record.getMessage()
    if message != expected.message:
        return f"record message {message!r}, expected {expected.message!r}"
    if record.exc_info is None:
        if expected.exc_type is not None:
            return f"record must carry {expected.exc_type.__name__} in exc_info"
    elif record.exc_info[0] is not expected.exc_type:
        # ``exc_info[0]`` is optional, so name whatever type is actually there.
        actual_name = getattr(record.exc_info[0], "__name__", None)
        return (
            f"record exc_info {actual_name}, "
            f"expected {getattr(expected.exc_type, '__name__', None)!r}"
        )
    return _extra_mismatch("record", record, expected.extra)


def _record_key(
    record: logging.LogRecord,
) -> tuple[int, str, tuple[tuple[str, object], ...]]:
    """Return a canonical key, for order-insensitive comparison."""
    return (
        record.levelno,
        record.getMessage(),
        tuple((key, getattr(record, key, None)) for key in _RECORD_FIELDS),
    )


def _expected_key(
    expected: ExpectedRecord,
) -> tuple[int, str, tuple[tuple[str, object], ...]]:
    """Return the canonical key of an expected record."""
    return (
        expected.level,
        expected.message,
        tuple((key, expected.extra.get(key)) for key in _RECORD_FIELDS),
    )


def _dispatch_records(
    records: cabc.Iterable[logging.LogRecord],
) -> list[logging.LogRecord]:
    """Return the records the dispatcher owns, in emission order."""
    return [
        record
        for record in records
        if record.name == _LOGGER_NAME
        and record.getMessage().startswith(DISPATCH_MESSAGE_PREFIXES)
    ]


def _log_mismatch(
    records: cabc.Iterable[logging.LogRecord],
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> str | None:
    """Check every dispatch record against the specification."""
    actual = _dispatch_records(records)
    ordered_expected, lifecycle_expected = expected_records(expectation, event)
    ordered_actual = [
        record
        for record in actual
        if not record.getMessage().startswith(_LIFECYCLE_PREFIXES)
    ]
    lifecycle_actual = [
        record
        for record in actual
        if record.getMessage().startswith(_LIFECYCLE_PREFIXES)
    ]
    if len(ordered_actual) != len(ordered_expected):
        return (
            f"{len(ordered_actual)} scheduling/failure records, "
            f"expected {len(ordered_expected)}"
        )
    ordered = _first_mismatch(
        starmap(_record_mismatch, zip(ordered_actual, ordered_expected, strict=True))
    )
    if ordered is not None:
        return ordered
    if sorted(map(_record_key, lifecycle_actual)) != sorted(
        map(_expected_key, lifecycle_expected)
    ):
        return "the task lifecycle records do not match the specification"
    return None


async def mismatch(
    dispatch: _Dispatch,
    kinds: cabc.Sequence[str],
    event: ExecEvent,
    records: cabc.Iterable[logging.LogRecord],
) -> str | None:
    """Return the first way ``dispatch`` departs from the specification."""
    expectation = expected_dispatch(kinds, event, settled=event.phase == "settled")
    probes = [make_probe(kind) for kind in kinds]
    outcome = _run_dispatch(dispatch, tuple(probe.hook for probe in probes), event)
    views = [_task_view(task) for task in outcome.tasks]
    # Settle first: an async hook records its call only when its coroutine
    # runs, so comparing calls before the tasks complete would report a
    # correctly scheduled hook as never called.
    await _settle(outcome.tasks)
    return (
        _call_mismatch(expectation, probes)
        or _error_mismatch(expectation, probes, outcome)
        or _task_mismatch(expectation, probes, views, event)
        or _log_mismatch(records, expectation, event)
    )


__all__ = ["make_event", "mismatch"]
