"""Model line-stream settlement across generated exit combinations.

``SafeCmd.lines()`` ends in more ways than ``run()``: completion, a timeout,
cancellation, an early ``aclose()``, and a setup failure all meet in
:func:`_settle_line_iteration`. That finalizer must close an opened session
exactly once, leave a declined or absent one alone, stop the delivery worker
before the session releases its framing, publish a genuine iteration failure
to observers, and map the ending onto the close outcome by a fixed precedence.

This machine drives the production finalizer over generated sink activation,
delivery mode, iteration failure, child result, and teardown failure, and checks
those obligations against an independent model of the precedence rule. The
real-child framing tests keep covering the leased workflow commands themselves;
this machine covers the settlement decisions taken around them.
"""

from __future__ import annotations

import asyncio
import typing as typ

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from cuprum import CommandResult, Program, sh
from cuprum._execution_tracking import _ExecutionTracking
from cuprum._line_stream_finalization import (
    _LineIterationState,
    _settle_line_iteration,
)
from cuprum._pipeline_types import _ExecutionHooks
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.events import TerminalOutcome
from cuprum.sinks import SessionStart
from cuprum.unittests._line_stream_settlement_support import (
    _Case,
    _OrderedSession,
    _RecordingWorker,
    _Settlement,
    _StubExecution,
    _StubObservation,
    _Teardown,
)
from cuprum.unittests._sink_test_support import RecordingSession, RecordingSink

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution

type _FailureKind = typ.Literal[
    "none",
    "generator-exit",
    "cancelled",
    "timeout",
    "error",
]
type _ResultKind = typ.Literal["none", "exit-zero", "exit-nonzero"]
type _SinkMode = typ.Literal["absent", "declined", "active"]

_FAILURE_KINDS: tuple[_FailureKind, ...] = (
    "none",
    "generator-exit",
    "cancelled",
    "timeout",
    "error",
)
_RESULT_KINDS: tuple[_ResultKind, ...] = ("none", "exit-zero", "exit-nonzero")
_SINK_MODES: tuple[_SinkMode, ...] = ("absent", "declined", "active")

_CHILD_EXIT = 7
_HOOK_ERROR = "observe hook failed"
_FUTURE_ERROR = "published result failed"


def _command_result(exit_code: int) -> CommandResult:
    """Build one completed child result for the settlement path."""
    return CommandResult(
        program=Program("settlement"),
        argv=(),
        exit_code=exit_code,
        pid=-1,
        stdout=None,
        stderr=None,
    )


def _failure_for(kind: _FailureKind) -> BaseException | None:
    """Build the iteration failure the generated case names."""
    match kind:
        case "generator-exit":
            return GeneratorExit()
        case "cancelled":
            return asyncio.CancelledError()
        case "timeout":
            return sh.TimeoutExpired(cmd="settlement", timeout=1.0)
        case "error":
            return RuntimeError("iteration failed")
        case _:
            return None


def _result_for(kind: _ResultKind) -> CommandResult | None:
    """Build the child result the generated case names."""
    match kind:
        case "exit-zero":
            return _command_result(0)
        case "exit-nonzero":
            return _command_result(_CHILD_EXIT)
        case _:
            return None


def _classify(error: BaseException) -> TerminalOutcome:
    """Map one error onto the terminal outcome the contract states."""
    match error:
        case sh.TimeoutExpired():
            return TerminalOutcome.TIMEOUT
        case asyncio.CancelledError() | GeneratorExit():
            return TerminalOutcome.CANCELLED
        case _:
            return TerminalOutcome.ERROR


def _model_outcome(
    result: CommandResult | None,
    failure: BaseException | None,
    teardown: BaseException | None,
) -> TerminalOutcome:
    """State the close outcome the precedence rule requires.

    A genuine iteration failure wins. Otherwise a result wins unless a
    teardown failure accompanied a clean exit, because the caller saw the
    teardown failure and the session must not report success. With neither, the
    close reports cancellation.

    Returns
    -------
    TerminalOutcome
        The terminal category the close must report.
    """
    if failure is not None and not isinstance(failure, GeneratorExit):
        return _classify(failure)
    result_outranks = result is not None and (result.exit_code != 0 or teardown is None)
    if result_outranks:
        return (
            TerminalOutcome.EXIT_ZERO
            if result.exit_code == 0
            else TerminalOutcome.EXIT_NONZERO
        )
    if teardown is not None:
        return _classify(teardown)
    return TerminalOutcome.CANCELLED


def _model_teardown(
    future_error: BaseException | None,
    hook_error: BaseException | None,
) -> BaseException | None:
    """State the teardown failure the drain is expected to surface."""
    if future_error is None:
        return hook_error
    if hook_error is None:
        return future_error
    # The drain groups the published failure with the hook failure, and a group
    # is an ordinary error for outcome mapping.
    return RuntimeError("grouped teardown failure")


async def _fail_on_first_tick(error: BaseException) -> None:
    """Raise the supplied error once the event loop runs the hook task."""
    await asyncio.sleep(0)
    raise error


async def _idle_coordinator() -> None:
    """Stand in for the coordinator task settlement has to absorb.

    Production only inspects the result future through the coordinator, so a
    started run must supply one for a staged future failure to surface.
    """


async def _settle_once(settlement: _Settlement) -> BaseException | None:
    """Settle one generated ending, returning any raised teardown failure."""
    execution = settlement.execution
    errors = settlement.errors
    result = settlement.result
    failure = settlement.failure
    result_future = (
        None if execution is None else asyncio.get_running_loop().create_future()
    )
    if result_future is not None:
        if errors.published is not None:
            result_future.set_exception(errors.published)
        elif result is not None:
            result_future.set_result(result)
    pending: list[asyncio.Task[None]] = []
    if errors.hook is not None:
        pending.append(asyncio.create_task(_fail_on_first_tick(errors.hook)))
    tracking = _ExecutionTracking(
        execution_hooks=_ExecutionHooks((), (), ()),
        pending_tasks=pending,
        sink_bracket=settlement.bracket,
    )
    state = _LineIterationState(
        execution=typ.cast("_SubprocessExecution | None", execution),
        result_future=result_future,
        coordinator=(
            None if execution is None else asyncio.create_task(_idle_coordinator())
        ),
    )
    try:
        await _settle_line_iteration(state, tracking, result, failure)
    except BaseException as error:  # ruff: ignore[blind-except] - the raised failure is the observation under test
        return error
    return None


class _SettlementMachine(RuleBasedStateMachine):
    """Drive the finalizer over generated sinks, endings, and teardowns."""

    def __init__(self) -> None:
        """Start with an unconfigured case; `prepare` fills it in."""
        super().__init__()
        self._case = _Case()

    @initialize(
        sink=st.sampled_from(_SINK_MODES),
        bounded=st.booleans(),
        started=st.booleans(),
    )
    def prepare(
        self,
        *,
        sink: _SinkMode,
        bounded: bool,
        started: bool,
    ) -> None:
        """Open one bracket and record the shape the model expects."""
        case = self._case
        case.started = started
        adapter = self._build_sink(sink)
        case.bracket = _SinkBracket.open(
            adapter,
            SessionStart(label="settlement", argv=("settlement",)),
            delivery_grace_s=0.05,
            bounded_delivery=bounded,
        )
        if started:
            case.worker = _RecordingWorker(case.order) if bounded else None
            case.execution = _StubExecution(
                _StubObservation(),
                case.worker,
            )

    def _build_sink(self, mode: _SinkMode) -> RecordingSink | None:
        """Build the adapter the generated sink mode names."""
        case = self._case
        if mode == "absent":
            return None
        if mode == "declined":
            return RecordingSink(decline=True)

        def factory() -> RecordingSession:
            """Create the ordered session this machine inspects."""
            session = _OrderedSession(case.order)
            case.session = session
            return session

        return RecordingSink(session_factory=factory)

    @precondition(lambda self: not self._case.settled)
    @rule(
        failure=st.sampled_from(_FAILURE_KINDS),
        result=st.sampled_from(_RESULT_KINDS),
        hook_fails=st.booleans(),
        future_fails=st.booleans(),
    )
    def settle(
        self,
        failure: _FailureKind,
        result: _ResultKind,
        *,
        hook_fails: bool,
        future_fails: bool,
    ) -> None:
        """Settle once and compare the close with the model's prediction."""
        case = self._case
        assert case.bracket is not None, "initialize must have opened a bracket"
        occurrence = _failure_for(failure)
        child_result = _result_for(result)
        hook_error = RuntimeError(_HOOK_ERROR) if case.started and hook_fails else None
        future_error = (
            RuntimeError(_FUTURE_ERROR) if case.started and future_fails else None
        )
        teardown = _model_teardown(future_error, hook_error)
        case.raised = asyncio.run(
            _settle_once(
                _Settlement(
                    bracket=case.bracket,
                    execution=case.execution,
                    result=child_result,
                    failure=occurrence,
                    errors=_Teardown(hook=hook_error, published=future_error),
                )
            )
        )
        case.settled = True
        case.result = child_result
        case.failure = occurrence
        case.expected = _model_outcome(child_result, occurrence, teardown)
        assert (case.raised is None) is (teardown is None), (
            f"a teardown failure must surface exactly when one was staged; "
            f"raised {case.raised!r} for {teardown!r}"
        )

    @precondition(lambda self: self._case.settled)
    @rule()
    def settle_again(self) -> None:
        """Settle a spent bracket again without reconciling anything twice."""
        case = self._case
        assert case.bracket is not None, "initialize must have opened a bracket"
        raised = asyncio.run(
            _settle_once(
                _Settlement(
                    bracket=case.bracket,
                    execution=None,
                    result=None,
                    failure=None,
                )
            )
        )
        assert raised is None, (
            f"re-settling a spent bracket must not fail; it raised {raised!r}"
        )

    @invariant()
    def session_closes_exactly_once(self) -> None:
        """Close an opened session once and leave an unopened one alone."""
        case = self._case
        if case.session is None:
            assert case.bracket is None or case.bracket.session is None, (
                "a run without an active adapter must not hold a session"
            )
            return
        expected_closes = 1 if case.settled else 0
        assert case.session.closed == expected_closes, (
            f"the session must close exactly {expected_closes} time(s); "
            f"it closed {case.session.closed}"
        )
        if not case.settled:
            return
        closed_with = case.session.closed_with
        assert closed_with is not None, "a settled run must record its outcome"
        assert closed_with.outcome is case.expected, (
            f"the close must report {case.expected}; it reported {closed_with.outcome}"
        )

    @invariant()
    def worker_closes_before_the_session(self) -> None:
        """Stop a started run's delivery worker before the session frame."""
        case = self._case
        if case.worker is None or not case.settled:
            return
        assert case.worker.closed == 1, (
            f"the worker must close exactly once; it closed {case.worker.closed}"
        )
        if case.session is not None:
            assert case.order == ["delivery", "session"], (
                f"the worker must close before the session; saw {case.order}"
            )

    @invariant()
    def failures_reach_the_observer(self) -> None:
        """Publish a genuine iteration failure as one terminal event."""
        case = self._case
        if case.execution is None or not case.settled:
            return
        expected = [] if case.failure is None else [_classify(case.failure)]
        assert case.execution.observation.terminals == expected, (
            f"observers must see {expected}; "
            f"they saw {case.execution.observation.terminals}"
        )


TestLineStreamSettlement = _SettlementMachine.TestCase
TestLineStreamSettlement.settings = settings(
    max_examples=40,
    stateful_step_count=12,
    deadline=None,
)
