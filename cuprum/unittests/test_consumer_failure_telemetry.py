"""What a consumer failure that ends a run reports, and where.

The rendezvous ends a run because one of its stream consumers failed, which is
a decision point: Cuprum terminates and reaps a child that was still working.
The channels that carry it are the ones the teardown already owns — an ERROR
``cuprum.timeout`` record, a ``teardown_error`` observe event naming the
failing stream and class, and the terminal ``settled`` event — and this module
pins that they fire on this route, once, with the callback's own exception
still reaching the caller unchanged.

A signal invented for the decision alone would be a new ``ExecPhase`` member,
and that literal is matched exhaustively by consumers: adding one makes an
adapter that was correct when it was written start raising. Reusing the
teardown channels keeps that contract intact, so the guarantee to pin is that
they are reached here rather than that some other channel exists.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import RunOutputOptions, sh
from cuprum.events import TerminalOutcome
from cuprum.unittests._wait4_process_support import _abandon
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.timeouts import started_pids

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd

# Long enough for a cold interpreter start, short enough that a run waiting on
# a blocked child fails here rather than looking like ordinary slowness.
_WATCHDOG_S = 10.0
# The child never stops writing, so nothing but the fail-fast path can end it
# once the callback has stopped reading.
_CHILD_SCRIPT = (
    "import sys\nwhile True:\n    print('endless', file=sys.{stream}, flush=True)\n"
)
# The channel the drain reports a failed reader on, named explicitly because
# operators filter on it and moving the code must not move the channel.
_DRAIN_LOGGER = "cuprum._subprocess_wait"


class _CallbackError(RuntimeError):
    """The exception the failing callback raises, carrying a fixed message."""

    def __init__(self) -> None:
        """Initialize with the message the case asserts reached the caller."""
        super().__init__("callback failed")


def _fail(_event: object) -> None:
    """Raise the callback's failure at the point the hook is invoked."""
    raise _CallbackError


def _only_started_pid(events: cabc.Sequence[ExecEvent], context: str) -> int:
    """Return the one child the run started, insisting there is exactly one."""
    pids = started_pids(events)
    assert len(pids) == 1, f"{context} must start exactly one child, got {pids!r}"
    return pids[0]


def _drive(command: SafeCmd, entry_point: str) -> None:
    """Drive one entry point to the callback failure it must report."""

    async def invoke() -> None:
        """Run the selected entry point until the callback raises."""
        if entry_point == "run":
            await command.run(output=RunOutputOptions(on_line=_fail))
            return
        async for _event in command.lines(output=RunOutputOptions(on_line=_fail)):
            pass

    asyncio.run(asyncio.wait_for(invoke(), _WATCHDOG_S))


def _teardown_events(events: cabc.Sequence[ExecEvent]) -> list[ExecEvent]:
    """Return the run's ``teardown_error`` events, of which there must be one."""
    reported = [event for event in events if event.phase == "teardown_error"]
    assert len(reported) == 1, (
        "a consumer failure that ends a run must be reported exactly once as a "
        f"teardown_error event, got {[event.phase for event in events]!r}"
    )
    return reported


def _drain_records(
    records: cabc.Sequence[logging.LogRecord],
) -> list[logging.LogRecord]:
    """Return the drain's own failure records, of which there must be one."""
    reported = [
        record
        for record in records
        if record.name == _DRAIN_LOGGER
        and record.getMessage().startswith("stream_consumer_failed")
    ]
    assert len(reported) == 1, (
        "a consumer failure that ends a run must produce exactly one "
        f"stream_consumer_failed record, got {[r.getMessage() for r in reported]!r}"
    )
    return reported


def _assert_reported(
    events: cabc.Sequence[ExecEvent],
    records: cabc.Sequence[logging.LogRecord],
    stream: str,
    context: str,
) -> None:
    """Assert every channel named the failure, its stream, and the run's end."""
    pid = _only_started_pid(events, context)
    event = _teardown_events(events)[0]
    assert event.pid == pid, (
        f"{context} must report the child it terminated, got {event.pid!r} for {pid!r}"
    )
    assert event.operation == "drain", (
        f"{context} must name the drain operation, got {event.operation!r}"
    )
    assert event.error_type == _CallbackError.__name__, (
        f"{context} must report the callback's class, got {event.error_type!r}"
    )
    assert stream in (event.note or ""), (
        f"{context} must name the failing stream in the event, got {event.note!r}"
    )

    record = _drain_records(records)[0]
    assert f"stream={stream}" in record.getMessage(), (
        f"{context} must name the failing stream in the drain record, got "
        f"{record.getMessage()!r}"
    )
    assert getattr(record, "cuprum_operation", None) == f"drain_{stream}", (
        f"{context} must label the drain record with the failing stream, got "
        f"{getattr(record, 'cuprum_operation', None)!r}"
    )

    terminal = [item for item in events if item.phase == "settled"]
    assert len(terminal) == 1, (
        f"{context} must still settle the run exactly once, got "
        f"{[item.phase for item in events]!r}"
    )
    assert terminal[0].terminal_outcome is TerminalOutcome.ERROR, (
        f"{context} must settle as a failed run, got {terminal[0].terminal_outcome!r}"
    )


@pytest.mark.parametrize("entry_point", ["run", "lines"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_early_consumer_failure_reports_every_channel(
    entry_point: str,
    stream: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A callback failure that ends the run is reported on every channel once."""
    context = f"{entry_point} after a {stream} callback failure"
    events: list[ExecEvent] = []
    command = build_python_builder()("-c", _CHILD_SCRIPT.format(stream=stream))
    caplog.set_level(logging.DEBUG)
    try:
        with sh.observe(events.append), pytest.raises(_CallbackError) as caught:
            _drive(command, entry_point)
        assert caught.value.args == ("callback failed",), (
            f"{context} must re-raise the callback's own exception, got "
            f"{caught.value!r}"
        )
        _assert_reported(events, caplog.records, stream, context)
    finally:
        # A run that failed to terminate its child must not leak it into the
        # rest of the suite; ``_abandon`` signals only a child this process
        # still owns, so a reaped pid that was recycled is left alone.
        for pid in started_pids(events):
            _abandon(pid)
