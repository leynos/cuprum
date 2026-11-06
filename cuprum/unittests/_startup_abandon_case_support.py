"""Instrumentation for the reclaim-orchestration properties.

The properties in ``test_startup_abandon_properties`` vary a small finite case
space — which of the sequence's steps fail, whether the failed setup had
started any tasks or resolved any capture, and where a cancellation lands — and
then read what the sequence did. The fakes and the substitution that produce
those cases are the same for every property, so they live here rather than
being rebuilt in each one.

The substitution happens one level *below* the sequence: the four steps
themselves run for real, and only what each one calls is replaced. That matters,
because the per-step suppression is exactly what the properties are about, so a
replacement that raised directly at a step would bypass the code under test.
Nothing here spawns a process or touches a descriptor.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ
from functools import partial
from unittest import mock

from cuprum import _subprocess_startup_abandon as reclaim

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import contextlib

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution

__all__ = [
    "_CAPTURE",
    "_STEP_NAMES",
    "_Case",
    "_Run",
    "_abandon",
    "_errors",
    "_expected_order",
    "_expected_reports",
    "_install_reporter",
    "_install_steps",
]

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

#: Stands in for a resolved capture accumulator. Its identity is what the
#: properties assert, so it must be one stable object rather than a fresh one
#: per attempt.
_CAPTURE = object()


class _Run(typ.NamedTuple):
    """What one reclaim attempt did, for the property to inspect.

    ``order`` records that a step was entered; ``settled`` and ``cleaned``
    record that the two steps whose work is asynchronous *completed*. The
    distinction matters under cancellation: a step can be entered and then
    abandoned, and only the completion lists can tell the two apart.
    """

    order: list[str]
    reported: list[str]
    capturers: list[tuple[object, ...]]
    settled: list[str]
    cleaned: list[tuple[object, ...]]


class _Case(typ.NamedTuple):
    """What one reclaim attempt was told to do, shared by every step stand-in.

    The stand-ins are substituted for what the real steps call, so each one is
    handed the whole case as a single argument. Keeping the fields together
    also lets a case pause one step without every other signature having to
    carry a gate it never reads, and lets a case omit the tasks or captures a
    failure early enough in the startup would never have created.
    """

    run: _Run
    failing: frozenset[str]
    errors: dict[str, BaseException]
    gate: asyncio.Event | None = None
    gate_step: str = "terminate"
    with_tasks: bool = True
    with_captures: bool = True


async def _pause(case: _Case, step: str) -> None:
    """Pause at *step* when the case gates there, so a caller can cancel."""
    if case.gate is not None and case.gate_step == step:
        case.gate.set()
        await asyncio.sleep(0.05)


class _FakeTask:
    """A cancellable stand-in for a task the failed setup had started.

    It records the cancellation, which is how the property observes that the
    settlement step ran, and fails it when the case asks. Its ``__await__``
    completes at once, so the real ``asyncio.gather`` inside the step still
    has something to settle.
    """

    def __init__(self, case: _Case) -> None:
        """Bind the case whose failure and gate this task should honour."""
        self._case = case

    def cancel(self) -> bool:
        """Record the cancellation, or fail the settlement step.

        The gate is released here rather than in ``__await__``: a case whose
        settlement fails raises before the gather is reached, so a gate opened
        only on the awaitable path would never fire and the cancelling case
        would wait for an event nothing sets.

        Returns
        -------
        bool
            Whether the cancellation request was recorded.
        """
        self._case.run.order.append("task_settle")
        if self._case.gate is not None and self._case.gate_step == "task_settle":
            self._case.gate.set()
        if "task_settle" in self._case.failing:
            raise self._case.errors["task_settle"]
        return True

    def __await__(self) -> cabc.Generator[None, None, None]:
        """Complete once gathered, pausing here if the case gates this step."""
        if self._case.gate is not None and self._case.gate_step == "task_settle":
            yield from asyncio.sleep(0.05).__await__()
        yield from ()
        self._case.run.settled.append("task_settle")


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


def _process() -> asyncio.subprocess.Process:
    """Return a child stub with no pipes and nothing to close."""
    return typ.cast(
        "asyncio.subprocess.Process",
        types.SimpleNamespace(pid=4242, stdin=None, stdout=None, stderr=None),
    )


async def _terminate(case: _Case, *_args: object, **_kwargs: object) -> None:
    """Stand in for the termination step, pausing at the case's gate."""
    case.run.order.append("terminate")
    await _pause(case, "terminate")
    if "terminate" in case.failing:
        raise case.errors["terminate"]


async def _discard(case: _Case, *_args: object) -> None:
    """Stand in for the output-discard step, yielding as the real drain does."""
    case.run.order.append("discard")
    await _pause(case, "discard")
    await asyncio.sleep(0)
    if "discard" in case.failing:
        raise case.errors["discard"]


async def _cleanup_captures(case: _Case, *args: object) -> None:
    """Stand in for the capture-cleanup step, recording what it was given."""
    case.run.order.append("capture_cleanup")
    case.run.capturers.append(typ.cast("tuple[object, ...]", args[0]))
    await _pause(case, "capture_cleanup")
    await asyncio.sleep(0)
    if "capture_cleanup" in case.failing:
        raise case.errors["capture_cleanup"]
    case.run.cleaned.append(typ.cast("tuple[object, ...]", args[0]))


def _install_steps(stack: contextlib.ExitStack, case: _Case) -> None:
    """Replace what each step calls, leaving the steps themselves intact.

    The substitution is one level *below* the sequence, so the per-step
    suppression the properties are about still runs.
    """
    for name, replacement in (
        ("_terminate_all_shielded", partial(_terminate, case)),
        ("_discard_outputs", partial(_discard, case)),
        ("_cleanup_capture_sinks", partial(_cleanup_captures, case)),
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


async def _abandon(case: _Case) -> None:
    """Run the reclaim against the stubs the case installed."""
    tasks = (_FakeTask(case),) if case.with_tasks else ()
    captures = (_CAPTURE,) if case.with_captures else ()
    await reclaim._abandon_started_process(
        _process(),
        _execution(),
        typ.cast("tuple[asyncio.Task[typ.Any], ...]", tasks),
        captures=typ.cast("tuple[CaptureSink | None, ...]", captures),
    )


def _errors() -> dict[str, BaseException]:
    """Return one distinct error per step, so identity can be asserted."""
    return {name: OSError(name) for name in _STEP_NAMES}


def _expected_order(*, with_tasks: bool) -> list[str]:
    """Return the steps that must run, given whether any task was started.

    Settlement returns before touching anything when the failed setup created no
    task, so that step alone is absent from the sequence in that shape.

    Returns
    -------
    list[str]
        The step names, in the order the sequence must run them.
    """
    return [name for name in _STEP_NAMES if with_tasks or name != "task_settle"]


def _expected_reports(failing: frozenset[str], *, with_tasks: bool) -> list[str]:
    """Return the operations the failing steps must report, in step order.

    Returns
    -------
    list[str]
        The reported operations, in the order the sequence must report them.
    """
    return [
        operation
        for name, operation in _STEPS
        if name in failing and (with_tasks or name != "task_settle")
    ]
