"""Unit tests for the Command-Query Separation helpers.

These cover the three helpers split or reshaped to obey CQRS:

- ``_enforce_allowlist`` (command) and ``_collect_hooks`` (query), the two
  halves of the former ``_run_before_hooks``;
- ``_emit_exec_event``, which now returns the tasks it scheduled rather than
  mutating a caller-supplied list; and
- ``_write_to_stream_writer``, which now reports downstream closure via a
  semantic ``_WriteOutcome`` and no longer closes the caller-owned writer.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import ECHO, LS, sh
from cuprum._observability import (
    _emit_exec_event,
    _wait_for_exec_hook_tasks,
)
from cuprum._pipeline_internals import _collect_hooks, _enforce_allowlist
from cuprum._pipeline_types import _EventDetails, _ExecutionHooks, _StageObservation
from cuprum._streams import _close_stream_writer, _write_to_stream_writer, _WriteOutcome
from cuprum.context import (
    ForbiddenProgramError,
    ScopeConfig,
    before,
    current_context,
    scoped,
)
from cuprum.unittests._cqrs_fixtures import _echo_cmd, _event

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent


class _AsyncObserveHookError(Exception):
    """Test exception raised by a scheduled async observe hook."""


class _SyncObserveHookError(Exception):
    """Test exception raised by a later synchronous observe hook."""


# -- _enforce_allowlist / _collect_hooks --------------------------------------


def test_enforce_allowlist_permits_allowed_program() -> None:
    """``_enforce_allowlist`` returns ``None`` for an allowed program."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        assert _enforce_allowlist(_echo_cmd()) is None, (
            "allowed programs should pass allowlist enforcement without a result"
        )


def test_enforce_allowlist_rejects_forbidden_program() -> None:
    """``_enforce_allowlist`` raises for a program outside the allowlist."""
    with (
        scoped(ScopeConfig(allowlist=frozenset([LS]))),
        pytest.raises(ForbiddenProgramError),
    ):
        _enforce_allowlist(_echo_cmd())


def test_collect_hooks_is_a_pure_query() -> None:
    """``_collect_hooks`` returns the context hooks without enforcing access."""

    def noop(_cmd: sh.SafeCmd) -> None:
        """Before-hook that records nothing."""

    # A restrictive allowlist that forbids ECHO must not affect the query:
    # collecting hooks is independent of allowlist enforcement.
    with scoped(ScopeConfig(allowlist=frozenset([LS]))), before(noop):
        ctx = current_context()
        hooks = _collect_hooks(ctx)

    assert hooks.before_hooks == ctx.before_hooks, (
        "collected before hooks should match the current context"
    )
    assert noop in hooks.before_hooks, (
        "collected before hooks should include the registered hook"
    )


def test_enforce_and_collect_are_independent() -> None:
    """Hook collection does not depend on, or trigger, allowlist enforcement."""
    with scoped(ScopeConfig(allowlist=frozenset([LS]))):
        ctx = current_context()
        # Collecting hooks for a forbidden command does not raise; only the
        # explicit command step enforces the allowlist.
        assert _collect_hooks(ctx).before_hooks == (), (
            "collecting hooks should not enforce a forbidden command"
        )
        with pytest.raises(ForbiddenProgramError):
            _enforce_allowlist(_echo_cmd())


# -- _emit_exec_event ---------------------------------------------------------


def test_emit_exec_event_returns_empty_for_sync_hooks() -> None:
    """Synchronous hooks schedule no tasks, so the return list is empty."""
    calls: list[ExecEvent] = []

    def sync_hook(event: ExecEvent) -> None:
        """Record the event synchronously."""
        calls.append(event)

    scheduled = _emit_exec_event((sync_hook,), _event())

    assert scheduled == [], "synchronous observe hooks should schedule no tasks"
    assert len(calls) == 1, "synchronous observe hook should be called once"


def test_emit_exec_event_returns_scheduled_async_tasks() -> None:
    """Async hooks are scheduled and returned for the caller to await."""

    async def run() -> None:
        """Drive the helper inside a running loop and await the tasks."""
        ran = asyncio.Event()

        async def async_hook(_event: ExecEvent) -> None:
            """Signal that the async hook executed."""
            await asyncio.sleep(0)
            ran.set()

        scheduled = _emit_exec_event((async_hook,), _event())
        assert len(scheduled) == 1, "one async hook should schedule one task"
        await asyncio.gather(*scheduled)
        assert ran.is_set(), "scheduled task must run the async hook"

    asyncio.run(run())


def test_stage_observation_preserves_scheduled_tasks_when_later_hook_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tasks scheduled before a hook failure remain pending for the caller."""

    async def run() -> None:
        """Emit through stage observation and await the preserved task."""
        pending_tasks: list[asyncio.Task[None]] = []

        async def async_hook(_event: ExecEvent) -> None:
            """Raise from an async hook that was scheduled before failure."""
            await asyncio.sleep(0)
            raise _AsyncObserveHookError

        def failing_hook(_event: ExecEvent) -> None:
            """Raise synchronously after the async hook schedules work."""
            raise _SyncObserveHookError

        observation = _StageObservation(
            cmd=_echo_cmd(),
            hooks=_ExecutionHooks(
                before_hooks=(),
                after_hooks=(),
                observe_hooks=(async_hook, failing_hook),
            ),
            tags={},
            cwd=None,
            env_overlay=None,
            pending_tasks=pending_tasks,
        )

        with (
            caplog.at_level(logging.WARNING, logger="cuprum._observability"),
            pytest.raises(_SyncObserveHookError),
        ):
            observation.emit("start", _EventDetails(pid=123))

        assert len(pending_tasks) == 1, "scheduled tasks must survive hook failure"
        with pytest.raises(_AsyncObserveHookError):
            await _wait_for_exec_hook_tasks(pending_tasks)
        assert pending_tasks == [], "wait helper should clear completed pending tasks"
        assert any(
            "observe_hook_failed" in record.message for record in caplog.records
        ), "synchronous observe hook failures should be logged"

    asyncio.run(run())


# -- _write_to_stream_writer --------------------------------------------------


class _RecordingWriter:
    """Minimal ``StreamWriter`` stand-in recording writes and closure."""

    def __init__(self, *, fail: bool) -> None:
        """Store whether ``drain`` should raise ``BrokenPipeError``."""
        self.chunks: list[bytes] = []
        self.closed = False
        self._fail = fail

    def write(self, chunk: bytes) -> None:
        """Record the written chunk."""
        self.chunks.append(chunk)

    async def drain(self) -> None:
        """Drain, optionally simulating a broken downstream pipe."""
        if self._fail:
            raise BrokenPipeError

    def close(self) -> None:
        """Record that the caller closed the writer."""
        self.closed = True


class _ClosingWriter:
    """Stream writer stand-in with configurable cleanup failures."""

    def __init__(
        self,
        *,
        write_eof_error: BaseException | None = None,
        close_error: BaseException | None = None,
        wait_closed_error: BaseException | None = None,
    ) -> None:
        """Store configured cleanup failures."""
        self.write_eof_error = write_eof_error
        self.close_error = close_error
        self.wait_closed_error = wait_closed_error
        self.closed = False

    def write_eof(self) -> None:
        """Signal EOF, optionally raising a configured cleanup error."""
        if self.write_eof_error is not None:
            raise self.write_eof_error

    def close(self) -> None:
        """Close the writer, optionally raising a configured cleanup error."""
        self.closed = True
        if self.close_error is not None:
            raise self.close_error

    async def wait_closed(self) -> None:
        """Wait for closure, optionally raising a configured cleanup error."""
        if self.wait_closed_error is not None:
            raise self.wait_closed_error


@pytest.mark.parametrize(
    ("fail", "expected_outcome"),
    [
        pytest.param(False, _WriteOutcome.OPEN, id="open-writer"),
        pytest.param(True, _WriteOutcome.CLOSED, id="broken-pipe"),
    ],
)
def test_write_to_stream_writer_reports_outcome(
    *,
    fail: bool,
    expected_outcome: _WriteOutcome,
) -> None:
    """Writes report downstream state without closing the writer."""

    async def run() -> _WriteOutcome:
        """Write one chunk to a configurable writer."""
        writer = _RecordingWriter(fail=fail)
        outcome = await _write_to_stream_writer(typ.cast("typ.Any", writer), b"payload")
        assert writer.chunks == [b"payload"], "writer should receive the payload chunk"
        assert not writer.closed, (
            "helper must leave writer ownership and closure to the caller"
        )
        return outcome

    assert asyncio.run(run()) is expected_outcome, (
        "write helper should report the expected downstream outcome"
    )


@pytest.mark.parametrize(
    ("writer", "expected_operation", "expected_error"),
    [
        pytest.param(
            _ClosingWriter(write_eof_error=BrokenPipeError()),
            "write_eof",
            "BrokenPipeError",
            id="write-eof",
        ),
        pytest.param(
            _ClosingWriter(close_error=ConnectionResetError()),
            "close",
            "ConnectionResetError",
            id="close",
        ),
        pytest.param(
            _ClosingWriter(wait_closed_error=BrokenPipeError()),
            "wait_closed",
            "BrokenPipeError",
            id="wait-closed",
        ),
    ],
)
def test_close_stream_writer_logs_suppressed_cleanup_errors(
    caplog: pytest.LogCaptureFixture,
    writer: _ClosingWriter,
    expected_operation: str,
    expected_error: str,
) -> None:
    """Suppressed stream cleanup errors are still visible in diagnostics."""
    with caplog.at_level(logging.DEBUG, logger="cuprum.streams"):
        asyncio.run(_close_stream_writer(typ.cast("typ.Any", writer)))

    matching_records = [
        record
        for record in caplog.records
        if "stream_writer_close_suppressed" in record.message
    ]
    assert matching_records, "suppressed cleanup errors should be logged"
    last_record = matching_records[-1]
    assert vars(last_record)["cuprum_operation"] == expected_operation, (
        "cleanup log should identify the suppressed operation"
    )
    assert vars(last_record)["cuprum_error_type"] == expected_error, (
        "cleanup log should identify the suppressed error type"
    )
