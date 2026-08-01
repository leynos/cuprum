"""The task-lifecycle contracts a pipeline's own byte movement depends on.

`_pipeline_pipe_tasks` owns the *tasks* that carry bytes: one pump per adjacent
stage pair, the capture tasks cancelled on teardown, and the collection of each
outcome. `_surface_unexpected_pipe_failures` then decides which of those
outcomes a pipeline tolerates — a downstream stage exiting early is expected
and must not fail the pipeline, while anything else stopped the data moving and
has to reach the caller.

These live beside the module they test rather than with the
descriptor-lifecycle fault injection, which is a separate concern.
"""

from __future__ import annotations

import asyncio
import enum
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._pipeline_pipe_tasks import (
    _collect_pipe_results,
    _create_pipe_tasks,
    _flatten_stream_tasks,
    _gather_optional_text_tasks,
    _surface_unexpected_pipe_failures,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_SUPPRESSED_PIPE_ERRORS = (BrokenPipeError, ConnectionResetError)


class _ResultTag(enum.StrEnum):
    """The outcomes a pipe task can deliver, as a closed set.

    An enum rather than a list of strings so a case cannot be added to the
    strategy without a matching arm below: an unhandled tag becomes a
    `ValueError` at generation time rather than silently falling through to
    whatever the last branch happened to return.
    """

    OK = "ok"
    BROKEN_PIPE = "broken_pipe"
    CONN_RESET = "conn_reset"
    VALUE_ERROR = "value_error"
    RUNTIME_ERROR = "runtime_error"
    CANCELLED = "cancelled"


def _make_pipe_result(tag: _ResultTag) -> object:
    """Materialize a pipe-task result for the given tag as a fresh object."""
    match tag:
        case _ResultTag.OK:
            return object()
        case _ResultTag.BROKEN_PIPE:
            return BrokenPipeError("downstream closed early")
        case _ResultTag.CONN_RESET:
            return ConnectionResetError("peer reset")
        case _ResultTag.VALUE_ERROR:
            return ValueError("unexpected pipe failure")
        case _ResultTag.RUNTIME_ERROR:
            return RuntimeError("unexpected pipe failure")
        case _ResultTag.CANCELLED:
            # A cancelled pump task. `CancelledError` derives from
            # `BaseException`, not `Exception`, so it is exactly the case an
            # `Exception` guard drops.
            return asyncio.CancelledError()


@given(tags=st.lists(st.sampled_from(list(_ResultTag)), max_size=8))
def test_surface_raises_first_unexpected_and_suppresses_pipe_errors(
    *,
    tags: list[_ResultTag],
) -> None:
    """The first non-pipe failure surfaces; pipe errors and values do not.

    The oracle is deliberately over ``BaseException``: a cancelled pump task
    delivered no bytes, so letting it pass as success would report a pipeline
    that never finished moving data as having completed.
    """
    results = [_make_pipe_result(tag) for tag in tags]
    unexpected = [
        result
        for result in results
        if isinstance(result, BaseException)
        and not isinstance(result, _SUPPRESSED_PIPE_ERRORS)
    ]

    if unexpected:
        with pytest.raises(
            (ValueError, RuntimeError, asyncio.CancelledError),
        ) as exc_info:
            _surface_unexpected_pipe_failures(results)
        assert exc_info.value is unexpected[0], (
            "the earliest unexpected exception must be the one raised"
        )
    else:
        # All results are either plain values or suppressed pipe errors.
        _surface_unexpected_pipe_failures(results)


class _StubStream:
    """A stand-in for a process stdout/stdin, identifiable by name."""

    def __init__(self, name: str) -> None:
        """Record the name this stub reports when a pump receives it."""
        self.name = name


class _StubProcess:
    """A process stand-in exposing only the stdio handles the pump reads."""

    def __init__(self, index: int) -> None:
        """Build distinguishable stdout and stdin handles for stage ``index``."""
        self.stdout = _StubStream(f"stdout-{index}")
        self.stdin = _StubStream(f"stdin-{index}")


@pytest.fixture(name="recorded_pumps")
def fixture_recorded_pumps(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, str]]:
    """Replace the pump with a recorder, returning the list of its arguments."""
    recorded: list[tuple[str, str]] = []

    # Async without awaiting: it substitutes for a coroutine function, so
    # `_create_pipe_tasks` can wrap it in a task the way it does the real one.
    async def fake_dispatch(reader: object, writer: object) -> None:  # noqa: RUF029
        """Record which pair of handles this hop was given."""
        recorded.append(
            (
                typ.cast("_StubStream", reader).name,
                typ.cast("_StubStream", writer).name,
            ),
        )

    monkeypatch.setattr(
        "cuprum._pipeline_pipe_tasks._pump_stream_dispatch",
        fake_dispatch,
    )
    return recorded


@pytest.mark.parametrize(
    ("stage_count", "expected"),
    [
        pytest.param(1, [], id="single-stage-has-no-hops"),
        pytest.param(2, [("stdout-0", "stdin-1")], id="one-hop"),
        pytest.param(
            4,
            [
                ("stdout-0", "stdin-1"),
                ("stdout-1", "stdin-2"),
                ("stdout-2", "stdin-3"),
            ],
            id="three-hops",
        ),
    ],
)
def test_a_pump_task_joins_each_adjacent_stage_pair(
    recorded_pumps: list[tuple[str, str]],
    stage_count: int,
    expected: list[tuple[str, str]],
) -> None:
    """One pump per adjacent pair, reading upstream and writing downstream.

    The direction is the part worth pinning: pumping ``stdin`` into ``stdout``,
    or joining stage *n* to stage *n + 2*, would still produce a plausible
    number of tasks.
    """

    async def drive() -> None:
        """Create the pump tasks and let them run to completion."""
        processes = typ.cast(
            "list[asyncio.subprocess.Process]",
            [_StubProcess(idx) for idx in range(stage_count)],
        )
        tasks = _create_pipe_tasks(processes)
        assert len(tasks) == max(0, stage_count - 1), (
            f"{stage_count} stages must yield {max(0, stage_count - 1)} hops, "
            f"found {len(tasks)}"
        )
        await asyncio.gather(*tasks)

    asyncio.run(drive())

    assert recorded_pumps == expected, (
        f"expected hops {expected!r}, found {recorded_pumps!r}"
    )


# Async without awaiting, so it can stand in for a real capture coroutine.
async def _immediate(value: str | None) -> str | None:  # noqa: RUF029
    """Return ``value`` from a task without touching real I/O."""
    return value


@pytest.mark.parametrize(
    ("stderr_present", "stdout_present", "expected"),
    [
        pytest.param([], False, [], id="nothing-running"),
        pytest.param([True, True], False, ["e0", "e1"], id="stderr-only"),
        pytest.param([], True, ["out"], id="stdout-only"),
        pytest.param([True, False, True], True, ["e0", "e2", "out"], id="mixed"),
    ],
)
def test_flatten_drops_absent_tasks_and_appends_stdout_last(
    stderr_present: list[bool],
    expected: list[str],
    *,
    stdout_present: bool,
) -> None:
    """Only running tasks are collected, with stdout last.

    ``_cancel_stream_tasks`` cancels whatever this returns, so a ``None``
    surviving the filter would raise on ``.cancel()`` during teardown.
    """

    async def drive() -> None:
        """Build the optional task set and flatten it."""
        stderr_tasks: list[asyncio.Task[str | None] | None] = [
            asyncio.create_task(_immediate(f"e{idx}")) if present else None
            for idx, present in enumerate(stderr_present)
        ]
        stdout_task = asyncio.create_task(_immediate("out")) if stdout_present else None

        flattened = _flatten_stream_tasks(stderr_tasks, stdout_task)

        assert all(task is not None for task in flattened), (
            "absent tasks must be dropped, not carried through as None"
        )
        assert [await task for task in flattened] == expected, (
            f"expected {expected!r} in order, found {flattened!r}"
        )

    asyncio.run(drive())


def test_gather_keeps_none_placeholders_aligned_with_inputs() -> None:
    """Absent capture tasks yield ``None`` in place, preserving stage order.

    The result is indexed by stage, so a missing capture must occupy its slot
    rather than shortening the tuple and shifting every later stage's output
    onto the wrong stage.
    """

    async def drive() -> tuple[str | None, ...]:
        """Await a mix of present and absent capture tasks."""
        tasks: list[asyncio.Task[str | None] | None] = [
            None,
            asyncio.create_task(_immediate("first")),
            None,
            asyncio.create_task(_immediate("second")),
            None,
        ]
        return await _gather_optional_text_tasks(tasks)

    assert asyncio.run(drive()) == (None, "first", None, "second", None), (
        "each absent task must hold its own slot so stage indices stay aligned"
    )


# Async without awaiting, so it can stand in for a real pump coroutine.
async def _failing(error: BaseException) -> None:  # noqa: RUF029
    """Raise ``error`` from a task, standing in for a failed pump."""
    raise error


def test_collect_returns_exceptions_in_task_order() -> None:
    """Every outcome is returned in order, failures included rather than raised.

    ``_surface_unexpected_pipe_failures`` raises the *first* unexpected
    failure, so order is part of the contract: collecting out of order would
    surface the wrong one. Failures must also be returned rather than raised
    here, or a single broken pipe would abort collection and lose the outcomes
    of every other hop.
    """
    first = ValueError("first")
    second = BrokenPipeError("second")

    async def drive() -> list[object]:
        """Collect a mix of successful and failing pipe tasks."""
        tasks: list[asyncio.Task[None]] = [
            asyncio.create_task(_failing(first)),
            asyncio.create_task(
                typ.cast("cabc.Coroutine[None, None, None]", _immediate(None)),
            ),
            asyncio.create_task(_failing(second)),
        ]
        return await _collect_pipe_results(tasks)

    results = asyncio.run(drive())

    assert results[0] is first, f"the first task's failure must come first: {results!r}"
    assert results[1] is None, f"a successful task must yield its value: {results!r}"
    assert results[2] is second, (
        f"the third task's failure must come third: {results!r}"
    )
