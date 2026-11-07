"""Instrumentation for the production-startup-state properties.

The properties in ``test_startup_abandon_startup_state`` fail the streamed
run's pre-hand-back setup at each of its boundaries and then read what the
reclaim was actually handed: the tasks the setup had started and the captures
it had resolved. The orchestration and the recording of each task are the real
ones; only their edges are replaced.

Every started task is a real asyncio task that nothing but a cancellation can
finish. That is what makes the properties sensitive to a task the run failed
to record: such a task is still pending when the run raises, which a fake
task's own bookkeeping could not show. The reclaim's steps are replaced one
level below the sequence, as in ``_startup_abandon_case_support``, so the
per-step suppression still runs. Nothing here spawns a process or touches a
descriptor.
"""

from __future__ import annotations

import asyncio
import time
import types
import typing as typ
from functools import partial
from unittest import mock

from cuprum import _subprocess_startup_abandon as reclaim
from cuprum import _subprocess_streams as streams
from cuprum._subprocess_stream_run import _run_subprocess_with_streams

if typ.TYPE_CHECKING:
    import contextlib

    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_wait_types import _StreamConsumerTask

__all__ = [
    "_BOUNDARIES",
    "_CAPTURES",
    "_RECLAIM_STEP_NAMES",
    "_SEQUENCE",
    "_TASKS_AT",
    "_BoundaryError",
    "_State",
    "_expected_begun",
    "_expected_captures",
    "_expected_reports",
    "_install",
    "_run",
    "_state",
]


class _BoundaryError(RuntimeError):
    """Raised by an injected setup failure at one boundary."""


#: The setup boundaries the streamed run can fail at, in the order they are
#: reached after the spawn. ``prepare`` is before any task exists; ``stdin`` is
#: the writer; the two ``consumer`` entries are the first and second stream
#: consumer; ``ownership`` is the assembly of the set the run retains.
_BOUNDARIES: tuple[str, ...] = (
    "prepare",
    "stdin",
    "consumer_1",
    "consumer_2",
    "ownership",
)

#: How many tasks the setup has started when each boundary fails. The writer is
#: first, so the boundaries before it have none, and ownership assembly is
#: last, so it has the writer and both consumers.
_TASKS_AT: dict[str, int] = {
    "prepare": 0,
    "stdin": 0,
    "consumer_1": 1,
    "consumer_2": 2,
    "ownership": 3,
}

#: The sequence's steps, in the order the module promises, and the operation a
#: failure of each is reported under. Task settlement is in the sequence but
#: absent here: the properties observe it from the tasks themselves, because a
#: settlement failure cannot be injected without replacing settlement itself.
_SEQUENCE: tuple[str, ...] = ("terminate", "task_settle", "discard", "capture_cleanup")

_RECLAIM_STEPS: tuple[tuple[str, str], ...] = (
    ("terminate", "terminate"),
    ("discard", "output_discard"),
    ("capture_cleanup", "capture_cleanup"),
)

_RECLAIM_STEP_NAMES: tuple[str, ...] = tuple(name for name, _ in _RECLAIM_STEPS)

#: One stable accumulator identity per stream, so a property can assert that
#: the very objects the setup resolved are the ones cleaned up.
_CAPTURE_STDOUT = object()
_CAPTURE_STDERR = object()
_CAPTURES: tuple[object, object] = (_CAPTURE_STDOUT, _CAPTURE_STDERR)


class _State(typ.NamedTuple):
    """What one failed startup and its reclaim did, for a property to inspect.

    ``started`` holds every task the setup actually created, recorded by the
    stand-ins at the moment the task was made rather than read from the run's
    own list. ``handed`` and ``cleaned`` split what the cleanup step was given
    from what it completed, so a step that was entered and then abandoned is
    distinguishable from one that ran.
    """

    boundary: str
    failing: frozenset[str]
    error: BaseException
    errors: dict[str, BaseException]
    started: list[asyncio.Task[None]]
    begun: list[str]
    reported: list[str]
    handed: list[tuple[object, ...]]
    cleaned: list[tuple[object, ...]]
    settled_at_discard: list[bool]
    gate: asyncio.Event | None = None
    gate_step: str = ""


def _state(
    boundary: str,
    *,
    failing: frozenset[str] = frozenset(),
    gate: asyncio.Event | None = None,
    gate_step: str = "",
) -> _State:
    """Build the record for one attempt, with a distinct error per step."""
    return _State(
        boundary=boundary,
        failing=failing,
        error=_BoundaryError("the setup failed"),
        errors={name: OSError(name) for name in _RECLAIM_STEP_NAMES},
        started=[],
        begun=[],
        reported=[],
        handed=[],
        cleaned=[],
        settled_at_discard=[],
        gate=gate,
        gate_step=gate_step,
    )


def _execution() -> _SubprocessExecution:
    """Return the minimum the setup and the reclaim read off a run bundle."""
    return typ.cast(
        "_SubprocessExecution",
        types.SimpleNamespace(
            ctx=types.SimpleNamespace(
                cancel_grace=1.0,
                encoding="utf-8",
                errors="strict",
            ),
            idle=None,
            observation=types.SimpleNamespace(
                monotonic_clock=time.monotonic,
                hooks=types.SimpleNamespace(observe_hooks=()),
            ),
            stdio=types.SimpleNamespace(
                stdin=object(),
                stdout=types.SimpleNamespace(is_pipe=True),
                stderr=types.SimpleNamespace(is_pipe=True),
            ),
            on_line=None,
            started_at=0.0,
            owns_process_group=False,
        ),
    )


def _process() -> asyncio.subprocess.Process:
    """Return a child stub with no pipes and nothing to close."""
    return typ.cast(
        "asyncio.subprocess.Process",
        types.SimpleNamespace(pid=4242, stdin=None, stdout=None, stderr=None),
    )


async def _never_finishes() -> None:
    """Wait on an event nothing sets, so only a cancellation ends the wait."""
    await asyncio.Event().wait()


def _blocked() -> asyncio.Task[None]:
    """Start a task that only a cancellation can finish."""
    return asyncio.ensure_future(_never_finishes())


def _spawn_inputs(state: _State) -> object:
    """Return the stream-input snapshot the run would have built."""
    context = types.SimpleNamespace(
        captures=_CAPTURES,
        relay_diagnostics=(object(), object()),
        stdout_config=object(),
        stderr_config=object(),
        pid=4242,
    )
    return types.SimpleNamespace(
        discard_on_cancel=asyncio.Event(),
        relay_diagnostics=context.relay_diagnostics,
        spawn_context=context,
    )


def _prepare(state: _State, *_args: object) -> object:
    """Build the stream inputs, or fail before any task exists."""
    if state.boundary == "prepare":
        raise state.error
    return _spawn_inputs(state)


def _spawn_stdin_writer(state: _State, *_args: object, **_kwargs: object) -> object:
    """Start the writer, or fail the boundary that lands before it."""
    if state.boundary == "stdin":
        raise state.error
    task = _blocked()
    state.started.append(task)
    return task


def _spawn_consumer(
    state: _State,
    calls: list[int],
    _wiring: object,
) -> _StreamConsumerTask | None:
    """Start one consumer, or fail the boundary that lands on it."""
    calls.append(len(calls) + 1)
    if state.boundary == "consumer_1" and len(calls) == 1:
        raise state.error
    if state.boundary == "consumer_2" and len(calls) == 2:
        raise state.error
    task = _blocked()
    state.started.append(task)
    return typ.cast("_StreamConsumerTask", task)


def _fail_ownership(state: _State, **_kwargs: object) -> object:
    """Fail the assembly step with every started task already recorded."""
    raise state.error


async def _pause(state: _State, step: str) -> None:
    """Pause at *step* when the attempt gates there, so its caller can cancel."""
    if state.gate is not None and state.gate_step == step:
        state.gate.set()
        await asyncio.sleep(0.05)


async def _terminate(state: _State, *_args: object, **_kwargs: object) -> None:
    """Stand in for the termination step."""
    state.begun.append("terminate")
    await _pause(state, "terminate")
    if "terminate" in state.failing:
        raise state.errors["terminate"]


async def _discard(state: _State, *_args: object) -> None:
    """Record whether every started task had settled before the pipes step."""
    state.begun.append("discard")
    state.settled_at_discard.append(
        all(task.done() for task in state.started),
    )
    await _pause(state, "discard")
    await asyncio.sleep(0)
    if "discard" in state.failing:
        raise state.errors["discard"]


async def _cleanup(state: _State, *args: object) -> None:
    """Stand in for the capture-cleanup step, recording what it was given."""
    state.begun.append("capture_cleanup")
    handed = typ.cast("tuple[object, ...]", args[0])
    state.handed.append(handed)
    await _pause(state, "capture_cleanup")
    await asyncio.sleep(0)
    if "capture_cleanup" in state.failing:
        raise state.errors["capture_cleanup"]
    state.cleaned.append(handed)


def _report(
    state: _State,
    *_owner: object,
    operation: str,
    error: BaseException,
) -> None:
    """Record the operation a suppressed failure was attributed to."""
    del error
    state.reported.append(operation)


def _install(stack: contextlib.ExitStack, state: _State) -> None:
    """Replace the setup's edges and the reclaim's steps for one attempt."""
    calls: list[int] = []
    for module, name, replacement in (
        (streams, "_prepare_stream_spawn", partial(_prepare, state)),
        (streams, "_spawn_stdin_writer", partial(_spawn_stdin_writer, state)),
        (streams, "_spawn_consumer", partial(_spawn_consumer, state, calls)),
        (reclaim, "_terminate_all_shielded", partial(_terminate, state)),
        (reclaim, "_discard_outputs", partial(_discard, state)),
        (reclaim, "_cleanup_capture_sinks", partial(_cleanup, state)),
        (reclaim, "_report_reclaim_failure", partial(_report, state)),
    ):
        stack.enter_context(mock.patch.object(module, name, replacement))
    if state.boundary == "ownership":
        stack.enter_context(
            mock.patch.object(
                streams,
                "_build_task_ownership",
                partial(_fail_ownership, state),
            )
        )


async def _run(state: _State) -> None:
    """Drive the streamed run's real pre-hand-back orchestration."""
    await _run_subprocess_with_streams(_process(), _execution(), pid=4242)


def _expected_begun() -> list[str]:
    """Return the reclaim steps every failed startup must enter, in order.

    Returns
    -------
    list[str]
        The step names, settlement excluded because the properties read it
        from the tasks' own state.
    """
    return [name for name in _SEQUENCE if name != "task_settle"]


def _expected_reports(state: _State) -> list[str]:
    """Return the operations the failing steps must report, in step order.

    Returns
    -------
    list[str]
        The reported operations, in the order the sequence must report them.
    """
    return [op for name, op in _RECLAIM_STEPS if name in state.failing]


def _expected_captures(state: _State) -> tuple[object, ...]:
    """Return the accumulators the reclaim must have been handed.

    Returns
    -------
    tuple[object, ...]
        The resolved pair, or the run's unresolved placeholder when the failure
        preceded the stream inputs.
    """
    return (None, None) if state.boundary == "prepare" else _CAPTURES
