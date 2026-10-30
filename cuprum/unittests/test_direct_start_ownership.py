"""Post-spawn ownership on the direct ``SafeCmd.run()`` path.

``_start_owned_process`` spawns the child and then emits the ``start`` event,
which runs synchronous observe hooks inline. A hook that raises there is not
swallowed: the failure escapes the spawn helper, so the caller never receives a
run to wait for, drain, or reap. These cases pin the ownership the startup
reclaim provides — the child is terminated and reaped, no writer or consumer
outlives the attempt, and the caller still meets the hook's own error.

The same reclaim covers the setup steps each run performs after the spawn: the
streamed run's consumer wiring and the direct run's stdin writer. Those are
injected here rather than reached through a fault, because a consumer factory
that fails is not something a child can be asked to do.

The child blocks without limit: it exists to be terminated, never to finish.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_direct_run, _subprocess_streams, scoped, sh
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    StdinInput,
)
from cuprum.unittests._start_ownership_support import (
    _BLOCKING_CHILD,
    _assert_strictly_reaped,
    _failing_start_hook,
    _python_catalogue,
    _recording_hook,
    _StartHookError,
)
from cuprum.unittests._wait4_process_support import (
    _abandon,
    _wait4_only,
)
from tests.helpers.timeouts import (
    child_argv,
    pending_tasks,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum._subprocess_streams import _ConsumerWiring
    from cuprum._subprocess_wait_types import _StreamConsumerTask
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


class _SetupError(RuntimeError):
    """Raised by an injected post-spawn setup failure."""


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("direct-start-ownership")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _output(*, streamed: bool) -> RunOutputOptions:
    """Return options that do or do not attach stream consumers."""
    if streamed:
        return RunOutputOptions()
    return RunOutputOptions(capture=False, echo=False)


def _fail_second_consumer(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Make the second stream consumer fail to start, recording each attempt.

    The first call is passed through, so a real stdout consumer is draining
    the child before the failure; the second raises. The returned log lets a
    caller prove that ordering rather than infer it from a passing assertion.

    Returns
    -------
    list[int]
        The one-based number of each consumer spawn attempt, in order.
    """
    original = _subprocess_streams._spawn_consumer
    calls: list[int] = []

    def failing(wiring: _ConsumerWiring) -> _StreamConsumerTask | None:
        """Start the first consumer, then refuse the second."""
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise _SetupError
        return original(wiring)

    monkeypatch.setattr(_subprocess_streams, "_spawn_consumer", failing)
    return calls


@pytest.mark.parametrize("with_stdin", [True, False], ids=["stdin", "no-stdin"])
def test_start_hook_failure_reaps_the_child_before_it_propagates(
    with_stdin: bool,
    tmp_path: Path,
) -> None:
    """A raising ``start`` hook cannot leave the direct child running."""
    _assert_start_hook_failure_reaps(
        streamed=False,
        with_stdin=with_stdin,
        tmp_path=tmp_path,
    )


@pytest.mark.parametrize("with_stdin", [True, False], ids=["stdin", "no-stdin"])
def test_streamed_start_hook_failure_reaps_the_child_before_it_propagates(
    with_stdin: bool,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """A raising ``start`` hook cannot leave the streamed child running."""
    _assert_start_hook_failure_reaps(
        streamed=True,
        with_stdin=with_stdin,
        tmp_path=tmp_path,
    )


def _assert_start_hook_failure_reaps(
    *,
    streamed: bool,
    with_stdin: bool,
    tmp_path: Path,
) -> None:
    """Run the failing-start case for one run shape and check the reclaim.

    ``start`` is emitted after the child exists and before the spawn helper
    returns, so a raising synchronous hook is the one failure that can strand a
    child nothing else holds. The caller must still meet that failure, and the
    child must be reaped — not merely signalled — by the time it does.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    stdin = StdinInput(text="unread payload") if with_stdin else None

    async def run_case() -> tuple[BaseException, set[asyncio.Task[object]]]:
        """Run the command and report the failure and the loop's leftovers."""
        with pytest.raises(_StartHookError) as caught:
            await command.run(output=_output(streamed=streamed), stdin=stdin)
        return typ.cast("BaseException", caught.value), pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_failing_start_hook(events)),
    ):
        error, still_pending = asyncio.run(run_case())

    pids = started_pids(events)
    try:
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        assert isinstance(error, _StartHookError), (
            f"the start-hook failure must reach the caller unwrapped, got {error!r}"
        )
        assert not still_pending, (
            f"a failed start must leave no task pending, got {still_pending!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a failed start hook")
    finally:
        for pid in pids:
            _abandon(pid)


def test_streamed_setup_failure_reclaims_the_started_consumer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """A consumer that fails to start must not strand the child or its sibling.

    The stdout consumer is already draining the child's pipe when stderr's
    creation fails, so the reclaim has to take both the child and the task
    started before the failure. Injecting the failure at the second consumer
    is what puts this case between those two facts.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    calls = _fail_second_consumer(monkeypatch)

    async def run_case() -> set[asyncio.Task[object]]:
        """Run the streamed command and report the loop's leftovers."""
        with pytest.raises(_SetupError):
            await command.run(output=RunOutputOptions())
        return pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_recording_hook(events)),
    ):
        still_pending = asyncio.run(run_case())

    pids = started_pids(events)
    try:
        assert calls == [1, 2], (
            f"the failure must land on the second consumer, got calls {calls!r}"
        )
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        assert not still_pending, (
            f"a failed streamed setup must leave no task pending, got {still_pending!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a failed streamed setup")
    finally:
        for pid in pids:
            _abandon(pid)


def test_direct_writer_setup_failure_reaps_the_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing stdin writer must not leave the direct run's child behind.

    The writer is the direct run's only task and creating it is the one setup
    step after its spawn, so a failure there hands the caller nothing that
    could reclaim the child. The startup reclaim is the only route left.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []

    def fail_writer(*_args: object, **_kwargs: object) -> None:
        """Refuse to create the run's stdin writer."""
        raise _SetupError

    monkeypatch.setattr(_subprocess_direct_run, "_spawn_stdin_writer", fail_writer)

    async def run_case() -> set[asyncio.Task[object]]:
        """Run the direct command and report the loop's leftovers."""
        with pytest.raises(_SetupError):
            await command.run(
                output=RunOutputOptions(capture=False, echo=False),
                stdin=StdinInput(text="unread payload"),
            )
        return pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_recording_hook(events)),
    ):
        still_pending = asyncio.run(run_case())

    pids = started_pids(events)
    try:
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        assert not still_pending, (
            f"a failed writer setup must leave no task pending, got {still_pending!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a failed stdin writer setup")
    finally:
        for pid in pids:
            _abandon(pid)


@_wait4_only
def test_owned_group_start_failure_reaps_the_child() -> None:
    """An owning run reclaims the child it spawned when its start hook fails.

    ``OWN_GROUP`` makes the teardown signal the child's process group rather
    than the single pid, so this pins that the startup reclaim carries the
    ownership the run was spawned under instead of falling back to the default.
    """
    command, catalogue = _command("-c", _BLOCKING_CHILD)
    events: list[ExecEvent] = []

    async def run_case() -> set[asyncio.Task[object]]:
        """Run the owned command and report the loop's leftovers."""
        with pytest.raises(_StartHookError):
            await command.run(
                output=RunOutputOptions(capture=False, echo=False),
                context=ExecutionContext(
                    process_group=ProcessGroupPolicy.OWN_GROUP,
                ),
            )
        return pending_tasks()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_failing_start_hook(events)),
    ):
        still_pending = asyncio.run(run_case())

    pids = started_pids(events)
    try:
        assert len(pids) == 1, (
            f"exactly one child must have been announced, got {pids!r}"
        )
        assert not still_pending, (
            f"a failed owned start must leave no task pending, got {still_pending!r}"
        )
        _assert_strictly_reaped(pids[0])
        wait_for_process_death(pids[0], context="a failed owned start hook")
    finally:
        for pid in pids:
            _abandon(pid)


def test_successful_start_delivers_the_child_unchanged() -> None:
    """A recording hook sees the child a normal run hands back.

    The reclaim must not disturb the success path: the announced pid is the
    run's own, the child's exit status and output are unchanged, and the
    lifecycle still opens at ``plan`` and settles.
    """
    command, catalogue = _command("-c", "print('child-ok')")
    events: list[ExecEvent] = []

    async def run_case() -> sh.CommandResult:
        """Run the command to completion."""
        return await command.run()

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(_recording_hook(events)),
    ):
        result = asyncio.run(run_case())

    phases = [event.phase for event in events]
    assert started_pids(events) == [result.pid], (
        f"the hook must observe the run's own child, got {started_pids(events)!r}"
    )
    assert result.exit_code == 0, f"the child must exit cleanly, got {result}"
    assert result.stdout == "child-ok\n", f"the child's output was lost: {result!r}"
    assert phases[:2] == ["plan", "start"], (
        f"a normal run must open at plan, then start, got {phases!r}"
    )
    assert phases[-1] == "settled", f"a normal run must settle, got {phases!r}"
