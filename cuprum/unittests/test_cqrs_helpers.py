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
import typing as typ

import pytest

from cuprum import ECHO, LS, sh
from cuprum._observability import _emit_exec_event
from cuprum._pipeline_internals import _collect_hooks, _enforce_allowlist
from cuprum._streams import _write_to_stream_writer, _WriteOutcome
from cuprum.context import (
    ForbiddenProgramError,
    ScopeConfig,
    before,
    current_context,
    scoped,
)
from cuprum.events import ExecEvent


def _echo_cmd() -> sh.SafeCmd:
    """Return a trivial ``SafeCmd`` for the allowlisted ``echo`` program."""
    return sh.make(ECHO)("hello")


# -- _enforce_allowlist / _collect_hooks --------------------------------------


def test_enforce_allowlist_permits_allowed_program() -> None:
    """``_enforce_allowlist`` returns ``None`` for an allowed program."""
    with scoped(ScopeConfig(allowlist=frozenset([ECHO]))):
        assert _enforce_allowlist(_echo_cmd()) is None


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

    assert hooks.before_hooks == ctx.before_hooks
    assert noop in hooks.before_hooks


def test_enforce_and_collect_are_independent() -> None:
    """Hook collection does not depend on, or trigger, allowlist enforcement."""
    with scoped(ScopeConfig(allowlist=frozenset([LS]))):
        ctx = current_context()
        # Collecting hooks for a forbidden command does not raise; only the
        # explicit command step enforces the allowlist.
        assert _collect_hooks(ctx).before_hooks == ()
        with pytest.raises(ForbiddenProgramError):
            _enforce_allowlist(_echo_cmd())


# -- _emit_exec_event ---------------------------------------------------------


def _event() -> ExecEvent:
    """Return a minimal ``plan`` event for hook dispatch."""
    return ExecEvent(
        phase="plan",
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={},
    )


def test_emit_exec_event_returns_empty_for_sync_hooks() -> None:
    """Synchronous hooks schedule no tasks, so the return list is empty."""
    calls: list[ExecEvent] = []

    def sync_hook(event: ExecEvent) -> None:
        """Record the event synchronously."""
        calls.append(event)

    scheduled = _emit_exec_event((sync_hook,), _event())

    assert scheduled == []
    assert len(calls) == 1


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


def test_write_to_stream_writer_reports_open_on_success() -> None:
    """A successful write reports ``OPEN`` and forwards the chunk."""

    async def run() -> _WriteOutcome:
        """Write one chunk to a healthy writer."""
        writer = _RecordingWriter(fail=False)
        outcome = await _write_to_stream_writer(typ.cast("typ.Any", writer), b"payload")
        assert writer.chunks == [b"payload"]
        assert not writer.closed, "helper must not close the caller-owned writer"
        return outcome

    assert asyncio.run(run()) is _WriteOutcome.OPEN


def test_write_to_stream_writer_reports_closed_on_broken_pipe() -> None:
    """A broken pipe reports ``CLOSED`` without closing the writer."""

    async def run() -> _WriteOutcome:
        """Write one chunk to a writer whose drain breaks."""
        writer = _RecordingWriter(fail=True)
        outcome = await _write_to_stream_writer(typ.cast("typ.Any", writer), b"payload")
        assert not writer.closed, (
            "helper must leave writer ownership and closure to the caller"
        )
        return outcome

    assert asyncio.run(run()) is _WriteOutcome.CLOSED
