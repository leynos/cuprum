"""The reclaim must be handed exactly the state the failed startup created.

``test_startup_abandon_properties`` states the reclaim's own sequence over
every pattern of step failure, but it builds that state itself. It therefore
cannot say whether the *production* startup hands the sequence the right state:
a run that started a task without recording it, or resolved capture
accumulators without passing them on, would satisfy those properties just the
same, and would leave both behind.

This module closes that gap. It fails the streamed run's real pre-hand-back
orchestration at each boundary — before the stream inputs exist, at the stdin
writer, at each consumer, and at ownership assembly — and then asserts that
every task the setup actually started was cancelled and settled, and that the
accumulators it resolved were the ones cleaned up. The mutation cases at the
end show the assertions are sensitive to the two hand-offs they describe.

Bounds and exclusions: the model covers ``_run_subprocess_with_streams`` only.
The ``start``-hook route (``_start_owned_process``) and the direct run
(``_run_subprocess_without_streams``) start no consumers and are covered by
their named cases. A settlement failure cannot be injected here without
replacing settlement itself, so it stays with the orchestrator properties. No
process is spawned and no descriptor is touched: the child and the stream
inputs are values, and the line-callback seam is left to production with an
observation that observes nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import _subprocess_stream_run
from cuprum import _subprocess_streams as streams
from cuprum.unittests._startup_state_case_support import (
    _BOUNDARIES,
    _CAPTURES,
    _RECLAIM_STEP_NAMES,
    _TASKS_AT,
    _BoundaryError,
    _expected_begun,
    _expected_captures,
    _expected_reports,
    _install,
    _run,
    _state,
)
from tests.helpers.timeouts import pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.unittests._startup_state_case_support import _State


def _assert_reclaimed(
    state: _State,
    statuses: list[tuple[bool, bool]],
    still_pending: set[asyncio.Task[object]],
) -> None:
    """Assert the reclaim settled every task and cleaned the resolved captures."""
    started = len(state.started)
    assert started == _TASKS_AT[state.boundary], (
        f"the {state.boundary} boundary must start {_TASKS_AT[state.boundary]} "
        f"tasks, so the case exercised what it claims; it started {started}"
    )
    assert statuses == [(True, True)] * started, (
        "every task the setup started must be done and cancelled before the "
        f"startup error reaches its caller, got {statuses!r}"
    )
    assert still_pending == set(), (
        "a task the run never recorded outlives the attempt; the reclaim must "
        f"leave nothing behind, got {still_pending!r}"
    )
    assert state.settled_at_discard == [True], (
        "settlement must complete before the pipes are released, so a reader "
        "cannot be still writing into an accumulator that is being removed"
    )
    assert state.begun == _expected_begun(), (
        f"the whole sequence must run in order, got {state.begun!r}"
    )
    expected_captures = _expected_captures(state)
    assert state.handed == [expected_captures], (
        "the cleanup must be handed the accumulators the setup resolved, "
        f"expected {[expected_captures]!r}, got {state.handed!r}"
    )
    expected_cleaned = [] if "capture_cleanup" in state.failing else [expected_captures]
    assert state.cleaned == expected_cleaned, (
        "a cleanup step that ran to completion must release them; one that "
        f"failed must not claim to have, got {state.cleaned!r}"
    )
    assert state.reported == _expected_reports(state), (
        "exactly the failing steps must be reported, in sequence order, got "
        f"{state.reported!r}"
    )


@given(
    boundary=st.sampled_from(_BOUNDARIES),
    failing=st.sets(st.sampled_from(_RECLAIM_STEP_NAMES)),
)
def test_every_boundary_hands_its_started_state_to_the_reclaim(
    boundary: str,
    failing: set[str],
) -> None:
    """Fail each setup boundary and inspect what the reclaim was handed.

    The caller must meet the setup's own error, unchanged, whichever cleanup
    steps also fail: a reclaim failure the caller never caused would misreport
    why the run ended.
    """
    state = _state(boundary, failing=frozenset(failing))

    async def run_case() -> tuple[
        BaseException,
        list[tuple[bool, bool]],
        set[asyncio.Task[object]],
    ]:
        """Run one attempt, returning the error, task states, and leftovers."""
        with contextlib.ExitStack() as stack:
            _install(stack, state)
            with pytest.raises(_BoundaryError) as caught:
                await _run(state)
            return (
                typ.cast("BaseException", caught.value),
                [(task.done(), task.cancelled()) for task in state.started],
                pending_tasks(),
            )

    raised, statuses, still_pending = asyncio.run(run_case())
    assert raised is state.error, (
        "the caller must meet the setup failure that ended the run, not a "
        f"reclaim failure that followed it, got {raised!r}"
    )
    _assert_reclaimed(state, statuses, still_pending)


@given(
    boundary=st.sampled_from(_BOUNDARIES),
    failing=st.sets(st.sampled_from(_RECLAIM_STEP_NAMES)),
    gate_step=st.sampled_from(_RECLAIM_STEP_NAMES),
)
def test_a_cancellation_is_deferred_until_the_started_state_is_settled(
    boundary: str,
    failing: set[str],
    gate_step: str,
) -> None:
    """Cancel the caller mid-sequence and require the reclaim to still finish.

    The sequence runs behind one shield, so the cancellation must wait rather
    than cut the reclaim short. The caller still meets ``CancelledError``, and
    only after every started task has settled.
    """

    async def run_case() -> tuple[
        _State,
        list[tuple[bool, bool]],
        set[asyncio.Task[object]],
    ]:
        """Cancel the caller inside the sequence and return what it left."""
        gate = asyncio.Event()
        state = _state(
            boundary,
            failing=frozenset(failing),
            gate=gate,
            gate_step=gate_step,
        )
        with contextlib.ExitStack() as stack:
            _install(stack, state)
            task = asyncio.ensure_future(_run(state))
            await gate.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return (
                state,
                [(started.done(), started.cancelled()) for started in state.started],
                pending_tasks(),
            )

    state, statuses, still_pending = asyncio.run(run_case())
    _assert_reclaimed(state, statuses, still_pending)


@given(mutation=st.sampled_from(("omitted", "delayed")))
def test_the_property_notices_a_task_the_setup_never_recorded(
    mutation: str,
) -> None:
    """Show the property fails when task recording is omitted or delayed.

    Both mutations are one-line changes to the recording step, and both leave
    a task running past the attempt. The assertions would then report a task
    that is neither done nor cancelled, which is exactly the defect the
    property exists to catch.
    """
    state = _state("ownership")
    held: list[asyncio.Task[None]] = []

    def record_nothing(_recorded: object, _task: object) -> None:
        """Drop every task, as a setup that never records one would."""

    def record_late(
        recorded: list[asyncio.Task[typ.Any]] | None,
        task: asyncio.Task[typ.Any] | None,
    ) -> None:
        """Record each task one step after it starts, losing the last one."""
        if recorded is None:
            return
        if held:
            recorded.append(held.pop())
        if task is not None:
            held.append(task)

    replacement = record_nothing if mutation == "omitted" else record_late

    async def run_case() -> tuple[list[tuple[bool, bool]], set[asyncio.Task[object]]]:
        """Run with the mutation installed and report what settled."""
        with contextlib.ExitStack() as stack:
            _install(stack, state)
            stack.enter_context(
                mock.patch.object(streams, "_record_started", replacement)
            )
            with pytest.raises(_BoundaryError):
                await _run(state)
            statuses = [(task.done(), task.cancelled()) for task in state.started]
            still_pending = pending_tasks()
            for task in state.started:
                task.cancel()
            await asyncio.gather(*state.started, return_exceptions=True)
            return statuses, still_pending

    statuses, still_pending = asyncio.run(run_case())
    recorded = 0 if mutation == "omitted" else _TASKS_AT["ownership"] - 1
    assert statuses.count((True, True)) == recorded, (
        f"only the {recorded} recorded tasks can be settled, got {statuses!r}"
    )
    assert still_pending != set(), (
        "a task the setup never recorded must still be running here; if it is "
        "not, this mutation is not exercising the hand-off it claims"
    )


def test_a_run_that_drops_its_captures_cleans_nothing() -> None:
    """Show the property fails when the resolved accumulators are not handed on.

    The streamed run resolves its accumulators and passes them to the reclaim.
    A run that dropped them would still reclaim its child and tasks, so only
    the capture assertions can distinguish it — which is why they assert on the
    identities the setup resolved rather than on a count.
    """
    state = _state("consumer_2")
    original = _subprocess_stream_run._abandon_started_process

    async def without_captures(
        process: asyncio.subprocess.Process,
        execution: _SubprocessExecution,
        tasks: cabc.Sequence[asyncio.Task[typ.Any]] = (),
        **_kwargs: object,
    ) -> None:
        """Reclaim the tasks but not the captures, as a dropped hand-off would."""
        await original(process, execution, tasks)

    async def run_case() -> tuple[list[tuple[object, ...]], list[tuple[bool, bool]]]:
        """Run with the captures dropped and report what the cleanup saw."""
        with contextlib.ExitStack() as stack:
            _install(stack, state)
            stack.enter_context(
                mock.patch.object(
                    _subprocess_stream_run,
                    "_abandon_started_process",
                    without_captures,
                )
            )
            with pytest.raises(_BoundaryError):
                await _run(state)
            return (
                state.handed,
                [(task.done(), task.cancelled()) for task in state.started],
            )

    handed, statuses = asyncio.run(run_case())
    assert handed == [()], (
        "this mutation must leave the cleanup with no accumulators; if it does "
        f"not, the property cannot see a dropped hand-off, got {handed!r}"
    )
    assert statuses == [(True, True)] * _TASKS_AT["consumer_2"], (
        "the tasks are still reclaimed; only the captures are lost, which is "
        "what makes the identity assertion the one that catches this"
    )
    assert handed != [_CAPTURES], "the property's own assertion would fail here"
