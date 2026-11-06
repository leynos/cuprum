"""The reclaim's orchestration contract, over every pattern of step failure.

The per-step behaviour of the startup reclaim is specified by named cases
against a real child. What those cases cannot say is what the *sequence* owes
when an arbitrary subset of its steps fails: each step must still get its turn,
each failure must be reported and then suppressed, and the error the caller is
already unwinding must reach it unchanged.

Those are relations over a small finite space — four steps, each failing or
not, in the shapes a failed startup can present — so they are stated here as
properties rather than as a case per combination. The instrumentation that
builds each case lives in ``_startup_abandon_case_support``; nothing here
spawns a process or touches a descriptor.
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum.unittests._startup_abandon_case_support import (
    _CAPTURE,
    _STEP_NAMES,
    _abandon,
    _Case,
    _errors,
    _expected_order,
    _expected_reports,
    _install_reporter,
    _install_steps,
    _Run,
)


@given(
    failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)),
    with_tasks=st.booleans(),
    with_captures=st.booleans(),
)
def test_every_step_gets_its_turn_and_reports_its_own_failure(
    failing: set[str],
    *,
    with_tasks: bool,
    with_captures: bool,
) -> None:
    """No failing step may skip a later one, or go unreported.

    A reclaim that stopped at its first own failure would leave the child or a
    descriptor behind, and one that swallowed a failure without reporting it
    would hide the leak from whoever reads the diagnostics. The two shapes
    matter as well: a startup that failed before creating a task has nothing to
    settle, and one that never resolved a capture has nothing to remove, so
    neither may invent a step or a report for it.
    """
    run = _Run(order=[], reported=[], capturers=[], settled=[], cleaned=[])
    frozen = frozenset(failing)
    errors = _errors()
    case = _Case(
        run,
        frozen,
        errors,
        with_tasks=with_tasks,
        with_captures=with_captures,
    )

    with contextlib.ExitStack() as stack:
        _install_reporter(stack, run)
        _install_steps(stack, case)

        async def run_case() -> None:
            """Abandon once, letting any escaped error reach this frame."""
            await _abandon(case)

        asyncio.run(run_case())

    assert run.order == _expected_order(with_tasks=with_tasks), (
        f"every applicable step must run in order; got {run.order!r} with "
        f"{failing!r} failing and tasks={with_tasks}"
    )
    assert run.reported == _expected_reports(frozen, with_tasks=with_tasks), (
        f"each suppressed failure must be reported once, in step order; "
        f"got {run.reported!r}, expected "
        f"{_expected_reports(frozen, with_tasks=with_tasks)!r}"
    )
    assert run.capturers == [(_CAPTURE,) if with_captures else ()], (
        "the reclaim must hand the caller's captures to the final step "
        f"unchanged, got {run.capturers!r}"
    )
    assert run.settled == (
        ["task_settle"] if with_tasks and "task_settle" not in frozen else []
    ), (
        "every task the failed setup started must actually settle; "
        f"got {run.settled!r} with tasks={with_tasks} and {failing!r} failing"
    )
    assert run.cleaned == (
        [] if "capture_cleanup" in frozen else [(_CAPTURE,) if with_captures else ()]
    ), (
        "the capture cleanup must complete for every sink it was handed; "
        f"got {run.cleaned!r} with captures={with_captures} and {failing!r} failing"
    )


@given(
    failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)),
    gate_step=st.sampled_from(_STEP_NAMES),
)
def test_a_cancelled_caller_still_completes_the_sequence(
    failing: set[str],
    gate_step: str,
) -> None:
    """A cancellation must not truncate the reclaim, wherever it lands.

    The whole sequence runs behind one shield, so a cancellation arriving while
    it is mid-step settles that step, runs the rest, and only then reaches the
    caller. Stopping early would strand whatever the remaining steps release.
    The gate is placed at each step in turn, because a shield that only held
    for the first await would pass a case that always cancelled there.
    """
    run = _Run(order=[], reported=[], capturers=[], settled=[], cleaned=[])
    frozen = frozenset(failing)
    errors = _errors()

    async def run_case() -> None:
        """Cancel the reclaim once the gated step is provably running."""
        gate = asyncio.Event()
        case = _Case(run, frozen, errors, gate, gate_step)
        with contextlib.ExitStack() as stack:
            _install_reporter(stack, run)
            _install_steps(stack, case)
            task = asyncio.ensure_future(_abandon(case))
            await gate.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_case())

    assert run.order == _expected_order(with_tasks=True), (
        f"a cancelled reclaim must still run every step; got {run.order!r} "
        f"with the cancellation at {gate_step!r}"
    )
    assert run.reported == _expected_reports(frozen, with_tasks=True), (
        f"the suppression pattern must not change under cancellation; "
        f"got {run.reported!r}, expected "
        f"{_expected_reports(frozen, with_tasks=True)!r}"
    )
    assert run.settled == ([] if "task_settle" in frozen else ["task_settle"]), (
        "the cancellation must not abort the settlement of the started task; "
        f"got {run.settled!r} with the gate at {gate_step!r}"
    )
    assert run.cleaned == ([] if "capture_cleanup" in frozen else [(_CAPTURE,)]), (
        "the cancellation must not abort the capture cleanup; "
        f"got {run.cleaned!r} with the gate at {gate_step!r}"
    )


def test_a_reclaim_where_every_step_fails_raises_nothing() -> None:
    """The caller's own error is the one it meets, never the reclaim's.

    The caller is already unwinding the failure that aborted the startup. Every
    step suppresses its own failure, so with all four failing the reclaim must
    still return normally and let that original error continue outward; one
    that let a teardown failure escape would misreport why the run ended. That
    is why nothing here catches: an escaping failure would fail this case
    outright, which is the outcome the property below rules out.
    """
    run = _Run(order=[], reported=[], capturers=[], settled=[], cleaned=[])
    frozen = frozenset(_STEP_NAMES)
    errors = _errors()
    case = _Case(run, frozen, errors)

    with contextlib.ExitStack() as stack:
        _install_reporter(stack, run)
        _install_steps(stack, case)
        asyncio.run(_abandon(case))

    assert run.order == _expected_order(with_tasks=True), (
        "every step's failure must be suppressed, not propagated"
    )
    assert run.reported == _expected_reports(frozen, with_tasks=True), (
        "each of the four failures must be reported rather than dropped"
    )


@given(failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)))
def test_the_callers_original_error_survives_the_reclaim(failing: set[str]) -> None:
    """The error being unwound must reach the caller as the same object.

    Suppression is not enough on its own: a reclaim that raised a *different*
    error, or wrapped the caller's, would still leave the caller unable to tell
    why the startup failed. The identity is what the call sites rely on when
    they re-raise, so it is asserted here rather than inferred from the
    absence of an exception.
    """
    run = _Run(order=[], reported=[], capturers=[], settled=[], cleaned=[])
    frozen = frozenset(failing)
    errors = _errors()
    case = _Case(run, frozen, errors)
    original = RuntimeError("the startup failed")

    def fail() -> None:
        """Raise the error the failing startup would be unwinding."""
        raise original

    async def run_case() -> None:
        """Fail as a startup would, reclaim, then re-raise the same error."""
        try:
            fail()
        except BaseException:
            await _abandon(case)
            raise

    with contextlib.ExitStack() as stack:
        _install_reporter(stack, run)
        _install_steps(stack, case)
        with pytest.raises(RuntimeError) as caught:
            asyncio.run(run_case())

    assert caught.value is original, (
        f"the caller must meet its own error unchanged, got {caught.value!r}"
    )
    assert run.order == _expected_order(with_tasks=True), (
        "the reclaim must still run every step before the error continues"
    )


@given(
    failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)),
    gate_step=st.sampled_from(_STEP_NAMES),
    bursts=st.integers(min_value=1, max_value=3),
)
def test_repeated_cancellation_never_shortens_the_sequence(
    failing: set[str],
    gate_step: str,
    bursts: int,
) -> None:
    """A caller that cancels again and again must still get the whole reclaim.

    One cancellation reaching the shield proves little about the next: a
    settle loop that stopped after the first would leave the sequence half
    run whenever the caller's cancellation was delivered more than once, which
    a timeout or a supervisor can do. Each burst cancels while the gated step
    is provably running, and the count is generated rather than fixed so a
    shield that only tolerated a single request cannot pass. Settlement and
    capture cleanup are asserted as completed, not merely entered: those are
    the two steps whose work continues asynchronously, and entering them is
    exactly what a truncated reclaim would still manage.
    """
    run = _Run(order=[], reported=[], capturers=[], settled=[], cleaned=[])
    frozen = frozenset(failing)
    errors = _errors()

    async def run_case() -> None:
        """Cancel the reclaim *bursts* times while the gated step runs."""
        gate = asyncio.Event()
        case = _Case(run, frozen, errors, gate, gate_step)
        with contextlib.ExitStack() as stack:
            _install_reporter(stack, run)
            _install_steps(stack, case)
            task = asyncio.ensure_future(_abandon(case))
            await gate.wait()
            for _ in range(bursts):
                task.cancel()
                await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_case())

    assert run.order == _expected_order(with_tasks=True), (
        f"every step must still run after {bursts} cancellations; got "
        f"{run.order!r} with the gate at {gate_step!r}"
    )
    assert run.reported == _expected_reports(frozen, with_tasks=True), (
        f"the suppression pattern must survive repeated cancellation; got "
        f"{run.reported!r}, expected {_expected_reports(frozen, with_tasks=True)!r}"
    )
    assert run.settled == ([] if "task_settle" in frozen else ["task_settle"]), (
        "the started task must settle despite repeated cancellation; "
        f"got {run.settled!r} after {bursts} cancellations"
    )
    assert run.cleaned == ([] if "capture_cleanup" in frozen else [(_CAPTURE,)]), (
        "the capture cleanup must complete despite repeated cancellation; "
        f"got {run.cleaned!r} after {bursts} cancellations"
    )
