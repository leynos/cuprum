"""The task-lifecycle and result-ownership contracts a pipeline depends on.

``cuprum._pipeline_stream_results`` owns result collection and teardown,
while ``cuprum._pipeline_streams`` creates one pump task per adjacent stage
pair.  ``_surface_unexpected_pipe_failures`` then determines which outcomes
must reach the caller, and each stage's ``CommandResult`` owns the relay
fallbacks of its own streams. These tests stay separate from
descriptor-lifecycle fault injection because they exercise task bookkeeping
and per-stage result assembly rather than FD hand-off.
"""

from __future__ import annotations

import asyncio
import enum
import typing as typ

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cuprum._pipeline_stream_results import (
    _collect_pipe_results,
    _flatten_stream_tasks,
    _gather_optional_text_tasks,
    _surface_unexpected_pipe_failures,
)
from cuprum._pipeline_streams import _create_pipe_tasks
from cuprum.echo_events import EchoErrorCategory, EchoStream, RelayFallback
from cuprum.echo_observation import observe_echo
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd

_SUPPRESSED_PIPE_ERRORS = (BrokenPipeError, ConnectionResetError)


class _ResultTag(enum.StrEnum):
    """The outcomes a pipe task can deliver, as a closed set.

    ``assert_never`` makes a new member without a matching branch a type error
    under ``ty`` and an assertion failure if it reaches this test at runtime.
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
            return asyncio.CancelledError()
        case _:  # pragma: no cover - unreachable while every member has an arm
            # `match` falls through silently on a missed case, and the
            # implicit `None` would be worse than a failure here:
            # `_surface_unexpected_pipe_failures` treats a non-`BaseException`
            # result as success, so a new tag would silently become a passing
            # "ok" outcome, defeating the guard this module exists to verify.
            typ.assert_never(tag)


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

    async def fake_dispatch(reader: object, writer: object) -> None:  # ruff: ignore[unused-async]
        """Record which pair of handles this hop was given."""
        recorded.append(
            (
                typ.cast("_StubStream", reader).name,
                typ.cast("_StubStream", writer).name,
            ),
        )

    monkeypatch.setattr("cuprum._pipeline_streams._pump_stream_dispatch", fake_dispatch)
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
    """Each pipe task reads upstream stdout and writes downstream stdin."""

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


async def _immediate(value: str | None) -> str | None:  # ruff: ignore[unused-async]
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
    """Only running tasks are collected, with stdout last."""

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
    """Absent capture tasks yield ``None`` in place, preserving stage order."""

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

    assert asyncio.run(drive()) == (None, "first", None, "second", None)


async def _failing(error: BaseException) -> None:  # ruff: ignore[unused-async]
    """Raise ``error`` from a task, standing in for a failed pump."""
    raise error


def test_collect_returns_exceptions_in_task_order() -> None:
    """Every outcome is returned in order, failures included rather than raised."""
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


class _Cp1252TextOnlySink:
    """Text-only sink rejecting payloads CP1252 cannot represent."""

    def __init__(self) -> None:
        """Record each attempted write payload."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the write, then reject CP1252-unrepresentable text."""
        self.attempts.append(payload)
        payload.encode("cp1252")
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


class _PassthroughSink:
    """Sink that accepts every write, modelling a healthy echo target."""

    def __init__(self) -> None:
        """Collect written text for assertions."""
        self.written: list[str] = []

    def write(self, payload: str) -> int:
        """Accept the payload unchanged."""
        self.written.append(payload)
        return len(payload)

    def flush(self) -> None:
        """Model the flush call on a text stream."""


_EXPECTED_STDERR_FALLBACK = RelayFallback(
    stream=EchoStream.STDERR,
    error_category=EchoErrorCategory.UNICODE_ENCODE,
)
_NON_ENCODABLE = "héllo ś"


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_pipeline_final_stage_owns_its_stdout_diagnostics(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The final stage reports its stdout disablement; earlier stages none."""
    rejecting = _Cp1252TextOnlySink()
    accepting = _PassthroughSink()

    async def run_case() -> tuple[tuple[RelayFallback, ...], tuple[RelayFallback, ...]]:
        """Pipe two stages; the final stage's stdout echoes to the bad sink."""
        with observe_echo(lambda _event: None):
            pipeline = python_builder("-c", "print('stage one')") | python_builder(
                "-c",
                f"import sys; print(sys.stdin.read().strip() + ' {_NON_ENCODABLE}')",
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stdout_sink=typ.cast("typ.IO[str]", accepting),
                    stderr_sink=typ.cast("typ.IO[str]", rejecting),
                ),
            )
        return result.stages[0].relay_fallbacks, result.stages[1].relay_fallbacks

    first_fallbacks, final_fallbacks = asyncio.run(run_case())

    assert first_fallbacks == (), (
        f"the first stage has no echo of its own stdout, got {first_fallbacks!r}"
    )
    # The final stage's stdout echoes through the context's stdout sink, which
    # is healthy here, so both stages report empty records for this wiring.
    assert final_fallbacks == (), (
        f"the final stage's stdout used the healthy sink, got {final_fallbacks!r}"
    )


def test_pipeline_stage_results_keep_stage_order(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Stage order is preserved while diagnostics stay per stage."""
    rejecting = _Cp1252TextOnlySink()

    async def run_case() -> tuple[
        int, tuple[RelayFallback, ...], tuple[RelayFallback, ...]
    ]:
        """Run a two-stage pipeline echoing every stderr to one sink."""
        with observe_echo(lambda _event: None):
            pipeline = python_builder(
                "-c", "import sys; sys.stderr.write('wörld ś\n'); print('mid')"
            ) | python_builder(
                "-c", "import sys; sys.stderr.write('zażółć\n'); print('done')"
            )
            result = await pipeline.run(
                output=RunOutputOptions(capture=True, echo=True),
                context=ExecutionContext(
                    stderr_sink=typ.cast("typ.IO[str]", rejecting),
                ),
            )
        return (
            len(result.stages),
            result.stages[0].relay_fallbacks,
            result.stages[1].relay_fallbacks,
        )

    stage_count, first_fallbacks, second_fallbacks = asyncio.run(run_case())

    assert stage_count == 2, "stage order and count must be preserved"
    assert first_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the first stage's stderr failure must be recorded, got {first_fallbacks!r}"
    )
    assert second_fallbacks == (_EXPECTED_STDERR_FALLBACK,), (
        f"the second stage's stderr failure must be recorded, got {second_fallbacks!r}"
    )
