"""Property tests for observe-hook dispatch.

The dispatcher must schedule exactly the results that
:func:`inspect.isawaitable` accepts, in order, up to the first failing hook.
The oracle in ``_observe_hook_dispatch_support`` states that specification
independently of the dispatcher, so these tests pass both before and after the
result-guarded fast path lands: they are a preservation property, not a red
test. The seeded-fault tests prove the property can reject a wrong dispatcher.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import logging
import types
import typing as typ

from hypothesis import HealthCheck, example, given, settings
from hypothesis import event as hypothesis_event
from hypothesis import strategies as st

from cuprum import ECHO
from cuprum._observability import (
    _await_awaitable,
    _emit_exec_event,
    _ExecEventEmissionError,
)
from cuprum.events import ExecEvent
from cuprum.unittests._observe_hook_dispatch_support import (
    HOOK_KINDS,
    DispatchExpectation,
    HookProbe,
    _close_result,
    expected_dispatch,
    make_probe,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.events import ExecHook, ExecPhase

_PHASES = ("plan", "start", "stdout", "exit")

# ``check_raises`` escapes the dispatcher unwrapped, so any task scheduled
# before it would be lost. It is exercised only as a sole hook, which still
# pins the unwrapped escape without leaking a pending task.
_NON_CHECK_KINDS = tuple(kind for kind in HOOK_KINDS if kind != "check_raises")

_KINDS = st.one_of(
    st.lists(st.sampled_from(_NON_CHECK_KINDS), min_size=0, max_size=8),
    st.just(["check_raises"]),
)

type _Dispatch = cabc.Callable[
    [tuple[ExecHook, ...], ExecEvent],
    list[asyncio.Task[None]],
]


def _event(phase: str) -> ExecEvent:
    """Return a minimal event for ``phase``."""
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


@dc.dataclass(frozen=True, slots=True)
class _Outcome:
    """The dispatcher's observable outcome, normalized for comparison."""

    error: BaseException | None
    wrapped: bool
    tasks: tuple[asyncio.Task[None], ...]


def _run_dispatch(
    dispatch: _Dispatch,
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> _Outcome:
    """Run ``dispatch`` and normalize its result or failure."""
    try:
        scheduled = dispatch(hooks, event)
    except _ExecEventEmissionError as exc:
        return _Outcome(
            error=exc.error,
            wrapped=True,
            tasks=tuple(exc.scheduled_tasks),
        )
    # The check failure and non-``Exception`` shapes escape unwrapped by
    # design, so every remaining failure is part of what must be compared.
    except BaseException as exc:  # ruff: ignore[blind-except]
        return _Outcome(error=exc, wrapped=False, tasks=())
    return _Outcome(error=None, wrapped=False, tasks=tuple(scheduled))


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
    failing = probes[len(expectation.called) - 1]
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
    outcome: _Outcome,
    event: ExecEvent,
) -> str | None:
    """Check the scheduled task count and names."""
    if len(outcome.tasks) != len(expectation.scheduled):
        return (
            f"scheduled {len(outcome.tasks)} tasks, "
            f"expected {len(expectation.scheduled)}"
        )
    expected_name = f"cuprum.observe.{event.phase}"
    for task in outcome.tasks:
        if task.get_name() != expected_name:
            return f"task named {task.get_name()!r}, expected {expected_name!r}"
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


def _scheduled_record_mismatch(
    index: int,
    record: logging.LogRecord,
    event: ExecEvent,
) -> str | None:
    """Check one scheduling record's level, logger, and structured fields."""
    if record.levelno != logging.DEBUG:
        return "a scheduling record must be DEBUG"
    if record.name != "cuprum._observability":
        return f"scheduling record logger {record.name!r}"
    return _extra_mismatch(
        "scheduling record",
        record,
        {
            "cuprum_phase": event.phase,
            "cuprum_program": str(event.program),
            "cuprum_scheduled_task_count": index,
        },
    )


def _scheduled_log_mismatch(
    records: cabc.Sequence[logging.LogRecord],
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> str | None:
    """Check the scheduling records against the specification."""
    scheduled = [
        record
        for record in records
        if record.getMessage().startswith("observe_hook_task_scheduled")
    ]
    if len(scheduled) != len(expectation.scheduled):
        return (
            f"{len(scheduled)} scheduling records, "
            f"expected {len(expectation.scheduled)}"
        )
    return _first_mismatch(
        _scheduled_record_mismatch(index, record, event)
        for index, record in enumerate(scheduled, start=1)
    )


def _failure_log_mismatch(
    records: cabc.Sequence[logging.LogRecord],
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> str | None:
    """Check the failure record against the specification."""
    failed = [
        record
        for record in records
        if record.getMessage().startswith("observe_hook_failed")
    ]
    if expectation.error is None or isinstance(
        expectation.error,
        asyncio.CancelledError,
    ):
        return "no failure record expected" if failed else None
    if len(failed) != 1:
        return f"{len(failed)} failure records, expected 1"
    record = failed[0]
    if record.levelno != logging.WARNING:
        return "a failure record must be WARNING"
    if record.exc_info is None or record.exc_info[0] is not type(expectation.error):
        return "a failure record must carry the raised exception type"
    return _extra_mismatch(
        "failure record",
        record,
        {
            "cuprum_phase": event.phase,
            "cuprum_program": str(event.program),
            "cuprum_error_type": type(expectation.error).__name__,
            "cuprum_scheduled_task_count": len(expectation.scheduled),
        },
    )


def _log_mismatch(
    records: cabc.Sequence[logging.LogRecord],
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> str | None:
    """Check the scheduling and failure records against the specification."""
    return _scheduled_log_mismatch(records, expectation, event) or (
        _failure_log_mismatch(records, expectation, event)
    )


async def _mismatch(
    dispatch: _Dispatch,
    kinds: cabc.Sequence[str],
    event: ExecEvent,
    records: cabc.Sequence[logging.LogRecord],
) -> str | None:
    """Return the first way ``dispatch`` departs from the specification."""
    expectation = expected_dispatch(kinds, event)
    probes = [make_probe(kind) for kind in kinds]
    outcome = _run_dispatch(dispatch, tuple(probe.hook for probe in probes), event)
    # Settle first: an async hook records its call only when its coroutine
    # runs, so comparing calls before the tasks complete would report a
    # correctly scheduled hook as never called.
    await _settle(outcome.tasks)
    return (
        _call_mismatch(expectation, probes)
        or _error_mismatch(expectation, probes, outcome)
        or _task_mismatch(expectation, outcome, event)
        or _log_mismatch(records, expectation, event)
    )


# ``caplog`` is function-scoped, so Hypothesis cannot reset it between
# generated inputs. The test clears it at the start of every example, which is
# the reset it needs; the health check would otherwise reject the fixture.
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@example(kinds=["sync_none"], phase="plan")
@example(kinds=["sync_value"], phase="plan")
@example(kinds=["plain_coroutine"], phase="plan")
@example(kinds=["lambda_coroutine"], phase="plan")
@example(kinds=["wraps_coroutine"], phase="plan")
@example(kinds=["async_def"], phase="plan")
@example(kinds=["partial_async"], phase="plan")
@example(kinds=["bound_async_method"], phase="plan")
@example(kinds=["async_call_instance"], phase="plan")
@example(kinds=["sync_call_instance"], phase="plan")
@example(kinds=["async_mock"], phase="plan")
@example(kinds=["autospec_async"], phase="plan")
@example(kinds=["marked_coroutine_function"], phase="plan")
@example(kinds=["future"], phase="plan")
@example(kinds=["generator_coroutine"], phase="plan")
@example(kinds=["awaitable_object"], phase="plan")
@example(kinds=["mock_result"], phase="plan")
@example(kinds=["magic_mock_result"], phase="plan")
@example(kinds=["raising"], phase="plan")
@example(kinds=["raising_fatal"], phase="plan")
@example(kinds=["raising_cancelled"], phase="plan")
@example(kinds=["check_raises"], phase="exit")
@example(kinds=["async_def", "async_def", "raising"], phase="stdout")
@given(kinds=_KINDS, phase=st.sampled_from(_PHASES))
def test_dispatch_matches_specification(
    kinds: list[str],
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Dispatch schedules exactly the awaitable results, in order."""
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="cuprum._observability")
    # Tag every generated sequence with the hook classes it exercised, so a
    # ``--hypothesis-show-statistics`` run proves the domain is not vacuous.
    for kind in kinds:
        hypothesis_event(kind)
    event = _event(phase)

    async def run() -> None:
        """Compare the dispatcher with the specification inside a loop."""
        mismatch = await _mismatch(_emit_exec_event, kinds, event, caplog.records)
        assert mismatch is None, f"{kinds} at {phase}: {mismatch}"

    asyncio.run(run())


def _schedule_when(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
    *,
    should_schedule: cabc.Callable[[object], bool],
) -> list[asyncio.Task[None]]:
    """Dispatch under a caller-supplied predicate, for seeded-fault tests."""
    scheduled: list[asyncio.Task[None]] = []
    for hook in hooks:
        result = hook(event)
        if not should_schedule(result):
            _close_result(result)
            continue
        scheduled.append(
            asyncio.create_task(
                _await_awaitable(typ.cast("cabc.Awaitable[None]", result), event.phase),
                name=f"cuprum.observe.{event.phase}",
            )
        )
    return scheduled


def _is_not_none(result: object) -> bool:
    """Accept every non-``None`` result: one seeded fault."""
    return result is not None


def _is_native_coroutine(result: object) -> bool:
    """Accept only a native coroutine: the other seeded fault."""
    return type(result) is types.CoroutineType


def test_specification_rejects_every_non_none_scheduled() -> None:
    """The property rejects a dispatcher that schedules a non-awaitable value."""

    async def run() -> None:
        """Observe the mismatch the wrong dispatcher must produce."""
        mismatch = await _mismatch(
            functools.partial(_schedule_when, should_schedule=_is_not_none),
            ["sync_value"],
            _event("plan"),
            (),
        )
        assert mismatch is not None, "a scheduled non-awaitable must be rejected"

    asyncio.run(run())


def test_specification_rejects_native_coroutines_only() -> None:
    """The property rejects a dispatcher that drops non-native awaitables."""

    async def run() -> None:
        """Observe a mismatch for each awaitable the wrong dispatcher drops."""
        for kind in ("future", "generator_coroutine", "awaitable_object"):
            mismatch = await _mismatch(
                functools.partial(_schedule_when, should_schedule=_is_native_coroutine),
                [kind],
                _event("plan"),
                (),
            )
            assert mismatch is not None, f"{kind} must be rejected"

    asyncio.run(run())
