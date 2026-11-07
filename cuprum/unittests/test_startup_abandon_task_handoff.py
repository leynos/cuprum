"""A failed streamed setup must cancel the tasks it had already started.

The streamed run starts its stdin writer and one consumer per stream after the
child exists and before it hands the run back. A failure part-way through that
setup escapes the run, so the reclaim is the only thing left that can settle
what was already started. The existing setup-failure cases prove the child is
reaped and that no task is left pending; neither can prove a *particular* task
was cancelled, because a task the run never handed over is simply never
created in those cases.

These cases start a task that only a cancellation can end, then fail the next
setup step. The task is asserted done and cancelled inside the running loop, so
a reclaim that was not handed it fails here rather than passing on a child that
happened to be reaped.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_streams, scoped, sh
from cuprum.sh import RunOutputOptions, StdinInput
from cuprum.unittests._start_ownership_support import (
    _assert_strictly_reaped,
    _fail_second_consumer,
    _python_catalogue,
    _recording_hook,
    _SetupError,
)
from cuprum.unittests._wait4_process_support import _abandon
from tests.helpers.timeouts import (
    child_argv,
    pending_tasks,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._subprocess_wait_types import _StreamConsumerTask
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


async def _blocked() -> None:
    """Wait to be cancelled; nothing else settles this task."""
    await asyncio.Event().wait()


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("startup-task-handoff")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _assert_child_reaped(events: list[ExecEvent]) -> None:
    """Assert exactly one child was announced, reaped, and is now dead."""
    pids = started_pids(events)
    try:
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a failed streamed setup")
    finally:
        for pid in pids:
            _abandon(pid)


def test_a_failed_consumer_start_cancels_the_consumer_already_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """The consumer started before the failure must be cancelled, not orphaned.

    The first consumer is replaced by a task that never finishes on its own, so
    the only way it can be done when the run raises is that the reclaim
    cancelled it. A run that started it without recording it would leave it
    pending, which is what the in-loop assertion reads.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    started: list[asyncio.Task[None]] = []
    calls: list[int] = []

    def spawn(_wiring: object) -> _StreamConsumerTask | None:
        """Start a blocking consumer first, then refuse the second."""
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise _SetupError
        task = asyncio.ensure_future(_blocked())
        started.append(task)
        return typ.cast("_StreamConsumerTask", task)

    monkeypatch.setattr(_subprocess_streams, "_spawn_consumer", spawn)

    async def run_case() -> tuple[set[asyncio.Task[object]], bool, bool]:
        """Run the streamed command and read the started task's state."""
        with pytest.raises(_SetupError):
            await command.run(output=RunOutputOptions())
        (task,) = started
        return pending_tasks(), task.done(), task.cancelled()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_recording_hook(events)),
    ):
        still_pending, is_done, is_cancelled = asyncio.run(run_case())

    assert calls == [1, 2], (
        f"the failure must land on the second consumer, got calls {calls!r}"
    )
    assert is_done, (
        "the consumer started before the failure must be settled by the time "
        "the run raises, or the reclaim was never handed it"
    )
    assert is_cancelled, (
        "only a cancellation can end a blocking consumer, so a settled task "
        "that is not cancelled was resumed instead"
    )
    assert not still_pending, (
        f"a failed streamed setup must leave no task pending, got {still_pending!r}"
    )
    _assert_child_reaped(events)


def test_a_failed_consumer_start_cancels_the_stdin_writer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """The writer started before the failure must be cancelled too.

    The writer is started before either consumer, so a failure on the second
    consumer is the first point at which the run can escape with a writer
    already running. The writer is replaced by a task that only a cancellation
    can end, so its state when the run raises is what proves the reclaim was
    handed it.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    writers: list[asyncio.Task[None]] = []

    def spawn_writer(*_args: object, **_kwargs: object) -> asyncio.Task[None]:
        """Start a blocking writer in place of the real one."""
        task = asyncio.ensure_future(_blocked())
        writers.append(task)
        return task

    monkeypatch.setattr(_subprocess_streams, "_spawn_stdin_writer", spawn_writer)
    calls = _fail_second_consumer(monkeypatch)

    async def run_case() -> tuple[set[asyncio.Task[object]], bool, bool]:
        """Run the streamed command and read the writer's state."""
        with pytest.raises(_SetupError):
            await command.run(
                output=RunOutputOptions(),
                stdin=StdinInput(text="unread payload"),
            )
        (task,) = writers
        return pending_tasks(), task.done(), task.cancelled()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_recording_hook(events)),
    ):
        still_pending, is_done, is_cancelled = asyncio.run(run_case())

    assert calls == [1, 2], (
        f"the failure must land on the second consumer, got calls {calls!r}"
    )
    assert is_done, (
        "the writer started before the failure must be settled by the time the "
        "run raises, or the reclaim was never handed it"
    )
    assert is_cancelled, (
        "only a cancellation can end a blocking writer, so a settled task that "
        "is not cancelled was resumed instead"
    )
    assert not still_pending, (
        f"a failed streamed setup must leave no task pending, got {still_pending!r}"
    )
    _assert_child_reaped(events)
