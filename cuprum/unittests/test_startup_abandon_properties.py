"""The reclaim's orchestration contract, over every pattern of step failure.

The per-step behaviour of the startup reclaim is specified by named cases
against a real child. What those cases cannot say is what the *sequence* owes
when an arbitrary subset of its steps fails: each step must still get its turn,
each failure must be reported and then suppressed, and the error the caller is
already unwinding must reach it unchanged.

Those are relations over a small finite space — four steps, each failing or
not — so they are stated here as properties rather than as sixteen cases. The
substitution happens one level *below* the sequence: the four steps themselves
run for real, and only what each one calls is replaced. That matters, because
the per-step suppression is exactly what these properties are about, so a
replacement that raised directly at a step would bypass the code under test.
Nothing here spawns a process or touches a descriptor.
"""

from __future__ import annotations

import asyncio
import contextlib
import types
import typing as typ
from functools import partial
from unittest import mock

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum import _subprocess_startup_abandon as reclaim

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution

#: The reclaim's steps, in the order the module promises, and the bounded
#: operation a failure of each is reported under. The order is the contract:
#: termination before settlement, settlement before the pipes, because a
#: consumer still draining could otherwise refill a pipe that was just
#: released.
_STEPS: tuple[tuple[str, str], ...] = (
    ("terminate", "terminate"),
    ("task_settle", "task_settle"),
    ("discard", "output_discard"),
    ("capture_cleanup", "capture_cleanup"),
)

_STEP_NAMES: tuple[str, ...] = tuple(name for name, _ in _STEPS)


class _Run(typ.NamedTuple):
    """What one reclaim attempt did, for the property to inspect."""

    order: list[str]
    reported: list[str]
    capturers: list[tuple[object, ...]]


class _FakeTask:
    """A cancellable stand-in for a task the failed setup had started.

    It records the cancellation, which is how the property observes that the
    settlement step ran, and fails it when the case asks. Its ``__await__``
    completes at once, so the real ``asyncio.gather`` inside the step still
    has something to settle.
    """

    def __init__(
        self,
        run: _Run,
        failing: frozenset[str],
        errors: dict[str, BaseException],
    ) -> None:
        """Bind the recorder and the failure this task should produce."""
        self._run = run
        self._failing = failing
        self._errors = errors

    def cancel(self) -> bool:
        """Record the cancellation, or fail the settlement step."""
        self._run.order.append("task_settle")
        if "task_settle" in self._failing:
            raise self._errors["task_settle"]
        return True

    def __await__(self) -> cabc.Generator[None, None, None]:
        """Complete immediately once the step gathers this task."""
        yield from ()


def _execution() -> _SubprocessExecution:
    """Return the minimum the steps read off a run bundle."""
    return typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            ctx=types.SimpleNamespace(cancel_grace=1.0),
            owns_process_group=False,
            observation=object(),
        ),
    )


def _process(run: _Run) -> asyncio.subprocess.Process:
    """Return a child stub with no pipes and nothing to close."""
    del run
    return typ.cast(
        "asyncio.subprocess.Process",
        types.SimpleNamespace(pid=4242, stdin=None, stdout=None, stderr=None),
    )


async def _terminate(
    run: _Run,
    failing: frozenset[str],
    errors: dict[str, BaseException],
    gate: asyncio.Event | None,
    *_args: object,
    **_kwargs: object,
) -> None:
    """Stand in for the termination step, pausing at *gate* when supplied.

    The pause is what lets a case cancel the caller while the sequence is
    provably mid-flight.
    """
    run.order.append("terminate")
    if gate is not None:
        gate.set()
        await asyncio.sleep(0.05)
    if "terminate" in failing:
        raise errors["terminate"]


async def _discard(
    run: _Run,
    failing: frozenset[str],
    errors: dict[str, BaseException],
    *_args: object,
) -> None:
    """Stand in for the output-discard step, yielding as the real drain does."""
    run.order.append("discard")
    await asyncio.sleep(0)
    if "discard" in failing:
        raise errors["discard"]


async def _cleanup_captures(
    run: _Run,
    failing: frozenset[str],
    errors: dict[str, BaseException],
    *args: object,
) -> None:
    """Stand in for the capture-cleanup step, recording what it was given."""
    run.order.append("capture_cleanup")
    run.capturers.append(typ.cast("tuple[object, ...]", args[0]))
    await asyncio.sleep(0)
    if "capture_cleanup" in failing:
        raise errors["capture_cleanup"]


def _install_steps(
    stack: contextlib.ExitStack,
    run: _Run,
    *,
    failing: frozenset[str],
    errors: dict[str, BaseException],
    gate: asyncio.Event | None = None,
) -> None:
    """Replace what each step calls, leaving the steps themselves intact.

    The substitution is one level *below* the sequence, so the per-step
    suppression these properties are about still runs.
    """
    for name, replacement in (
        ("_terminate_all_shielded", partial(_terminate, run, failing, errors, gate)),
        ("_discard_outputs", partial(_discard, run, failing, errors)),
        ("_cleanup_capture_sinks", partial(_cleanup_captures, run, failing, errors)),
    ):
        stack.enter_context(mock.patch.object(reclaim, name, replacement))


def _install_reporter(stack: contextlib.ExitStack, run: _Run) -> None:
    """Replace the failure reporter with one that records the operation."""

    def report(
        _process: object,
        _execution: object,
        *,
        operation: str,
        error: BaseException,
    ) -> None:
        """Record the operation a suppressed failure was attributed to."""
        del error
        run.reported.append(operation)

    stack.enter_context(
        mock.patch.object(reclaim, "_report_reclaim_failure", report),
    )


async def _abandon(
    run: _Run,
    failing: frozenset[str],
    errors: dict[str, BaseException],
) -> None:
    """Run the reclaim against the stubs the case installed."""
    tasks = typ.cast(
        "tuple[asyncio.Task[typ.Any], ...]",
        (_FakeTask(run, failing, errors),),
    )
    captures = typ.cast("tuple[CaptureSink | None, ...]", (object(),))
    await reclaim._abandon_started_process(
        _process(run),
        _execution(),
        tasks,
        captures=captures,
    )


def _errors() -> dict[str, BaseException]:
    """Return one distinct error per step, so identity can be asserted."""
    return {name: OSError(name) for name in _STEP_NAMES}


def _expected_reports(failing: frozenset[str]) -> list[str]:
    """Return the operations the failing steps must report, in step order."""
    return [operation for name, operation in _STEPS if name in failing]


@given(failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)))
def test_every_step_gets_its_turn_and_reports_its_own_failure(
    failing: set[str],
) -> None:
    """No failing step may skip a later one, or go unreported.

    A reclaim that stopped at its first own failure would leave the child or a
    descriptor behind, and one that swallowed a failure without reporting it
    would hide the leak from whoever reads the diagnostics.
    """
    run = _Run(order=[], reported=[], capturers=[])
    frozen = frozenset(failing)
    errors = _errors()

    with contextlib.ExitStack() as stack:
        _install_reporter(stack, run)
        _install_steps(stack, run, failing=frozen, errors=errors)

        async def run_case() -> None:
            """Abandon once, letting any escaped error reach this frame."""
            await _abandon(run, frozen, errors)

        asyncio.run(run_case())

    assert run.order == list(_STEP_NAMES), (
        f"every step must run in order; got {run.order!r} with {failing!r} failing"
    )
    assert run.reported == _expected_reports(frozen), (
        f"each suppressed failure must be reported once, in step order; "
        f"got {run.reported!r}, expected {_expected_reports(frozen)!r}"
    )
    assert len(run.capturers) == 1, (
        "the reclaim must hand every supplied capture to the final step"
    )
    assert len(run.capturers[0]) == 1, (
        "the capture the caller supplied must reach the cleanup step unchanged"
    )


@given(failing=st.sets(st.sampled_from(_STEP_NAMES), max_size=len(_STEP_NAMES)))
def test_a_cancelled_caller_still_completes_the_sequence(
    failing: set[str],
) -> None:
    """A cancellation must not truncate the reclaim, and must still propagate.

    The whole sequence runs behind one shield, so a cancellation arriving while
    it is mid-step settles that step, runs the rest, and only then reaches the
    caller. Stopping early would strand whatever the remaining steps release.
    """
    run = _Run(order=[], reported=[], capturers=[])
    frozen = frozenset(failing)
    errors = _errors()

    async def run_case() -> None:
        """Cancel the reclaim once its first step is provably running."""
        gate = asyncio.Event()
        with contextlib.ExitStack() as stack:
            _install_reporter(stack, run)
            _install_steps(stack, run, failing=frozen, errors=errors, gate=gate)
            task = asyncio.ensure_future(_abandon(run, frozen, errors))
            await gate.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(run_case())

    assert run.order == list(_STEP_NAMES), (
        f"a cancelled reclaim must still run every step; got {run.order!r}"
    )
    assert run.reported == _expected_reports(frozen), (
        f"the suppression pattern must not change under cancellation; "
        f"got {run.reported!r}, expected {_expected_reports(frozen)!r}"
    )


def test_a_reclaim_where_every_step_fails_raises_nothing() -> None:
    """The caller's own error is the one it meets, never the reclaim's.

    The caller is already unwinding the failure that aborted the startup. Every
    step suppresses its own failure, so with all four failing the reclaim must
    still return normally and let that original error continue outward; one
    that let a teardown failure escape would misreport why the run ended. That
    is why nothing here catches: an escaping failure would fail this case
    outright, which is the outcome the properties below rule out.
    """
    run = _Run(order=[], reported=[], capturers=[])
    frozen = frozenset(_STEP_NAMES)
    errors = _errors()

    with contextlib.ExitStack() as stack:
        _install_reporter(stack, run)
        _install_steps(stack, run, failing=frozen, errors=errors)
        asyncio.run(_abandon(run, frozen, errors))

    assert run.order == list(_STEP_NAMES), (
        "every step's failure must be suppressed, not propagated"
    )
    assert run.reported == _expected_reports(frozen), (
        "each of the four failures must be reported rather than dropped"
    )
