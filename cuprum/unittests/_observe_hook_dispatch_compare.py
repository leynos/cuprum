"""Compare observe-hook dispatch against the specification oracle.

These helpers run the real dispatcher over a sequence of hooks and report the
first way its behaviour departs from :mod:`_observe_hook_dispatch_oracle`. The
comparison covers which hooks were called and in what order, the surfaced
failure, and the scheduled tasks — count, order, name, wrapper and wrapped
result. The log records such a dispatch must emit are compared in
:mod:`_observe_hook_dispatch_logs`.

A failing awaitability check escapes unwrapped, so the tasks scheduled before it
are recorded as they are created, compared like any others, and settled
afterwards.

The task view is captured *before* the scheduled tasks are awaited: a completed
coroutine drops its frame, so the wrapper identity and the wrapped result are
only observable at scheduling time.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ
from itertools import starmap
from unittest import mock

from cuprum import ECHO
from cuprum._observability import _await_awaitable, _ExecEventEmissionError
from cuprum.events import ExecEvent
from cuprum.unittests._observe_hook_dispatch_logs import _log_mismatch
from cuprum.unittests._observe_hook_dispatch_oracle import (
    DispatchExpectation,
    expected_dispatch,
    scheduled_probe_indices,
)
from cuprum.unittests._observe_hook_dispatch_support import HookProbe, make_probe

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextvars
    import logging

    from cuprum.events import ExecHook, ExecPhase

type _Dispatch = cabc.Callable[
    [tuple[ExecHook, ...], ExecEvent],
    list[asyncio.Task[None]],
]

# One hook call as the comparison sees it: which probe ran, and of what kind.
type _TracedCall = tuple[int, str]


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


@contextlib.contextmanager
def _recorded_tasks(
    created: list[asyncio.Task[None]],
) -> cabc.Iterator[None]:
    """Record every task the synchronously running dispatcher creates.

    A failing awaitability check escapes unwrapped, so its caller never
    receives the tasks scheduled before it. Recording creation is the only way
    to compare those tasks and settle them afterwards; settling matters
    because a task left pending would outlive the example.

    Yields
    ------
    None
        While ``asyncio.create_task`` is instrumented. The dispatcher runs
        synchronously, so nothing else creates a task in this window.
    """
    real_create_task = asyncio.create_task

    def record(
        coro: cabc.Coroutine[object, object, None],
        *,
        name: str | None = None,
        context: contextvars.Context | None = None,
    ) -> asyncio.Task[None]:
        """Create the task, record it, and return it unchanged."""
        task = real_create_task(coro, name=name, context=context)
        created.append(task)
        return task

    with mock.patch.object(asyncio, "create_task", record):
        yield


def _run_dispatch(
    dispatch: _Dispatch,
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> _Outcome:
    """Run ``dispatch`` and normalize its result or failure."""
    created: list[asyncio.Task[None]] = []
    try:
        with _recorded_tasks(created):
            scheduled = dispatch(hooks, event)
    except _ExecEventEmissionError as exc:
        return _Outcome(error=exc.error, wrapped=True, tasks=tuple(exc.scheduled_tasks))
    # The check failure and non-``Exception`` shapes escape unwrapped by
    # design, so every remaining failure is part of what must be compared.
    # The tasks created before the escape are compared too, rather than
    # dropped, so a dispatcher that lost one is reported.
    except BaseException as exc:  # ruff: ignore[blind-except]
        return _Outcome(error=exc, wrapped=False, tasks=tuple(created))
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


def _traced_hooks(
    probes: cabc.Sequence[HookProbe],
    trace: list[_TracedCall],
) -> tuple[ExecHook, ...]:
    """Wrap each probe's hook so ``trace`` records which probe ran, in order.

    The probe's position identifies it. Its kind cannot: two probes of the same
    kind trace identically, so a dispatcher that ran them in reverse would
    otherwise look correct.

    Returns
    -------
    tuple[ExecHook, ...]
        One hook per probe, in the order the dispatcher must call them.
    """

    def traced(index: int, probe: HookProbe) -> ExecHook:
        """Return ``probe``'s hook, recording the call before delegating."""

        def hook(event: ExecEvent) -> object:
            """Record which probe ran, then deliver the event to it."""
            trace.append((index, probe.kind))
            return probe.hook(event)

        return typ.cast("ExecHook", hook)

    return tuple(starmap(traced, enumerate(probes)))


def _call_mismatch(
    expectation: DispatchExpectation,
    probes: cabc.Sequence[HookProbe],
    trace: cabc.Sequence[_TracedCall],
) -> str | None:
    """Check that each hook was called exactly when the specification says.

    Per-hook counts name a hook that ran when it should not have. The ordered
    trace names a dispatcher that called the right hooks in the wrong order,
    which no count can see; it records each probe's position, so two probes of
    one kind are still told apart.

    Returns
    -------
    str | None
        The first way a call departs from the specification, or ``None``.
    """
    for index, probe in enumerate(probes):
        expected = 1 if index < len(expectation.called) else 0
        if len(probe.calls) != expected:
            return (
                f"{probe.kind} was called {len(probe.calls)} times, expected {expected}"
            )
    expected_calls = tuple(enumerate(expectation.called))
    if tuple(trace) != expected_calls:
        return f"hooks ran as {tuple(trace)}, expected {expected_calls}"
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


async def mismatch(
    dispatch: _Dispatch,
    kinds: cabc.Sequence[str],
    event: ExecEvent,
    records: cabc.Iterable[logging.LogRecord],
) -> str | None:
    """Return the first way ``dispatch`` departs from the specification."""
    expectation = expected_dispatch(kinds, event, settled=event.phase == "settled")
    trace: list[_TracedCall] = []
    probes = [make_probe(kind) for kind in kinds]
    outcome = _run_dispatch(dispatch, _traced_hooks(probes, trace), event)
    views = [_task_view(task) for task in outcome.tasks]
    # Settle first: an async hook records its call only when its coroutine
    # runs, so comparing calls before the tasks complete would report a
    # correctly scheduled hook as never called.
    await _settle(outcome.tasks)
    return (
        _call_mismatch(expectation, probes, trace)
        or _error_mismatch(expectation, probes, outcome)
        or _task_mismatch(expectation, probes, views, event)
        or _log_mismatch(records, expectation, event)
    )


__all__ = ["make_event", "mismatch"]
