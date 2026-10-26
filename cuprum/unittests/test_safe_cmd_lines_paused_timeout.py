"""A paused ``lines()`` iterator still settles within its deadline.

The bounded event queue applies backpressure while a caller is consuming
promptly. Once teardown begins, however, a blocked queue publication must not
prevent the run from preserving its capture and settling its readers. These
cases leave the caller idle across expiry with a full queue, then ask for the
next item and verify that the recorded timeout reaches it.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import sys
import time
import typing as typ

import pytest

from cuprum._line_stream import _LINE_QUEUE_CAPACITY
from cuprum._process_group import _POST_KILL_SETTLEMENT_S
from cuprum._stream_drain import _CAPTURE_EOF_GRACE_S
from cuprum.line_stream_events import LineStreamPhase
from cuprum.line_stream_observation import observe_line_stream
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    SafeCmd,
    TimeoutExpired,
)
from cuprum.unittests._post_exit_teardown_support import (
    RUN_BUDGET_S,
    assert_reclaimed,
    owned_builder,
    reap_descendant,
    recorded_descendant_or_fail,
)
from tests.helpers.timeouts import descendant_then_exit_argv, pending_tasks

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.line_stream_events import LineStreamEvent
    from cuprum.lines import LineEvent

# pytest resolves this imported fixture by name at collection time.
__all__ = ["owned_builder"]

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process groups are unavailable on Windows",
)

_DEADLINE_S = 1.0
_CANCEL_GRACE_S = 0.1
_OUTPUT_OPTIONS = RunOutputOptions(capture=True, echo=False)
_OWNED_SETTLEMENT_LIMIT_S = (
    _DEADLINE_S + _CANCEL_GRACE_S + _CAPTURE_EOF_GRACE_S + _POST_KILL_SETTLEMENT_S + 1.0
)


@dc.dataclass(frozen=True, slots=True)
class _LineStreamScenario:
    """The command and expected capture for one paused iterator regression."""

    command: SafeCmd
    first_line: str
    expected_output: str
    pid_file: Path


@dc.dataclass(frozen=True, slots=True)
class _LineStreamPayload:
    """The output and child marker needed to construct one scenario command."""

    pid_file: Path
    first_line: str
    stdout_chunks: tuple[str, ...]
    expected_output: str


@dc.dataclass(frozen=True, slots=True)
class _PausedRunOutcome:
    """Evidence collected after one paused line stream reaches its deadline."""

    first_line: LineEvent
    expired: TimeoutExpired
    pending_tasks: set[asyncio.Task[object]]
    elapsed: float
    descendant_pid: int


async def _read_first_line_then_pause(
    command: SafeCmd,
    *,
    settled: asyncio.Event,
) -> tuple[LineEvent, TimeoutExpired, set[asyncio.Task[object]], float]:
    """Pause after one delivered line, then observe its recorded expiry."""
    started = time.monotonic()
    stream = command.lines(
        output=_OUTPUT_OPTIONS,
        timeout=_DEADLINE_S,
        context=ExecutionContext(
            cancel_grace=_CANCEL_GRACE_S,
            process_group=ProcessGroupPolicy.OWN_GROUP,
        ),
    )
    first = await asyncio.wait_for(anext(stream), timeout=RUN_BUDGET_S)
    await asyncio.wait_for(settled.wait(), timeout=RUN_BUDGET_S)
    elapsed = time.monotonic() - started
    with pytest.raises(TimeoutExpired) as caught:
        await asyncio.wait_for(anext(stream), timeout=RUN_BUDGET_S)
    return first, caught.value, pending_tasks(), elapsed


def _line_stream_scenario(
    owned_builder: cabc.Callable[..., SafeCmd],
    payload: _LineStreamPayload,
) -> _LineStreamScenario:
    """Build one pipe-holding command for a paused iterator case."""
    command = owned_builder(
        *descendant_then_exit_argv(
            payload.pid_file,
            exit_code=0,
            stdout_chunks=payload.stdout_chunks,
        )
    )
    return _LineStreamScenario(
        command,
        payload.first_line,
        payload.expected_output,
        payload.pid_file,
    )


def _complete_line_scenario(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> _LineStreamScenario:
    """Build complete lines that fill the queue after the first is delivered."""
    lines = tuple(f"complete-{index:03}" for index in range(_LINE_QUEUE_CAPACITY + 2))
    expected = "\n".join(lines) + "\n"
    return _line_stream_scenario(
        owned_builder,
        _LineStreamPayload(
            pid_file=tmp_path / "paused-lines-descendant.pid",
            first_line=lines[0],
            stdout_chunks=(expected,),
            expected_output=expected,
        ),
    )


def _unterminated_tail_scenario(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> _LineStreamScenario:
    """Build a full queue followed by a final fragment in a separate chunk."""
    lines = tuple(f"complete-{index:03}" for index in range(_LINE_QUEUE_CAPACITY + 1))
    complete_output = "\n".join(lines) + "\n"
    trailing = "unterminated-tail"
    expected = complete_output + trailing
    return _line_stream_scenario(
        owned_builder,
        _LineStreamPayload(
            pid_file=tmp_path / "paused-tail-descendant.pid",
            first_line=lines[0],
            stdout_chunks=(complete_output, trailing),
            expected_output=expected,
        ),
    )


async def _run_owned_paused_case(
    scenario: _LineStreamScenario,
) -> _PausedRunOutcome:
    """Expire one paused iterator, verify group cleanup, then reap its child."""
    settled = asyncio.Event()

    def observe(event: LineStreamEvent) -> None:
        """Record teardown completion without waking the paused iterator."""
        if event.phase is LineStreamPhase.TEARDOWN_COMPLETED:
            settled.set()

    descendant_pid: int | None = None
    try:
        with observe_line_stream(observe):
            first, expired, pending, elapsed = await _read_first_line_then_pause(
                scenario.command,
                settled=settled,
            )
        descendant_pid = recorded_descendant_or_fail(scenario.pid_file)
        await assert_reclaimed(
            descendant_pid,
            "the timeout completed but left the owned descendant holding "
            "the line stream's pipe alive",
        )
        return _PausedRunOutcome(first, expired, pending, elapsed, descendant_pid)
    finally:
        reap_descendant(scenario.pid_file, descendant_pid)


def test_full_line_queue_keeps_capture_when_a_paused_reader_expires(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """A blocked complete-line publication cannot discard its captured chunk."""
    scenario = _complete_line_scenario(owned_builder, tmp_path)
    outcome = asyncio.run(_run_owned_paused_case(scenario))

    assert outcome.first_line.text == scenario.first_line
    assert outcome.expired.output == scenario.expected_output, (
        "expiry must preserve the bytes captured before the blocked queue "
        f"publication; got {outcome.expired.output!r}"
    )
    assert outcome.descendant_pid > 0, "the command must leave a live pipe holder"
    assert outcome.elapsed < _OWNED_SETTLEMENT_LIMIT_S, (
        f"the paused line stream settled too slowly: {outcome.elapsed:.2f}s"
    )
    assert not outcome.pending_tasks, (
        f"the timed-out line stream left tasks pending: {outcome.pending_tasks!r}"
    )


def test_unterminated_tail_does_not_block_timeout_delivery(
    owned_builder: cabc.Callable[..., SafeCmd],
    tmp_path: Path,
) -> None:
    """A trailing fragment cannot re-block the drain after read cancellation."""
    scenario = _unterminated_tail_scenario(owned_builder, tmp_path)
    outcome = asyncio.run(_run_owned_paused_case(scenario))

    assert outcome.first_line.text == scenario.first_line
    assert outcome.descendant_pid > 0, "the command must leave a live pipe holder"
    assert outcome.expired.output == scenario.expected_output, (
        "the final unterminated fragment and preceding capture must survive "
        f"timeout settlement; got {outcome.expired.output!r}"
    )
    assert outcome.elapsed < _OWNED_SETTLEMENT_LIMIT_S, (
        f"the paused line stream settled too slowly: {outcome.elapsed:.2f}s"
    )
    assert not outcome.pending_tasks, (
        f"the timed-out line stream left tasks pending: {outcome.pending_tasks!r}"
    )
