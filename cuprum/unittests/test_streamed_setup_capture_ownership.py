"""A failed streamed setup must hand its capture sinks to the reclaim.

The streamed run resolves both streams' capture accumulators before it starts
the consumers, and a consumer that fails to start leaves those accumulators
holding spill files. The reclaim removes them, but only if the run passes them
on: the helper cannot discover a sink the failed setup never handed over.

The existing setup-failure case proves the child and the started consumer are
reclaimed. It cannot prove the sinks arrived, because a run that dropped them
would still reap the child and leave no task pending — the spill file would
simply survive. This case watches the hand-off itself, at the seam between the
run and the helper, and asserts both sinks are present and distinct.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_stream_run, scoped, sh
from cuprum.sh import RunOutputOptions
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
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._capture_types import CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("streamed-setup-captures")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


def _record_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[CaptureSink | None, ...]]:
    """Record the capture sinks each reclaim is handed, then reclaim as usual.

    Returns
    -------
    list[tuple[CaptureSink | None, ...]]
        One entry per reclaim call, holding the sinks that call received.
    """
    original = _subprocess_stream_run._abandon_started_process
    recorded: list[tuple[CaptureSink | None, ...]] = []

    async def recording(
        process: asyncio.subprocess.Process,
        execution: _SubprocessExecution,
        tasks: cabc.Sequence[asyncio.Task[typ.Any]] = (),
        *,
        captures: cabc.Iterable[CaptureSink | None] = (),
    ) -> None:
        """Record the hand-off, then perform the real reclaim."""
        recorded.append(tuple(captures))
        await original(process, execution, tasks, captures=captures)

    monkeypatch.setattr(
        _subprocess_stream_run,
        "_abandon_started_process",
        recording,
    )
    return recorded


def test_streamed_setup_failure_hands_both_capture_sinks_to_the_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """Both resolved accumulators must reach the reclaim, not just the child.

    The failure lands on the second consumer, which is after both stream
    configs were resolved. A run that passed an empty ``captures`` would still
    reap the child and leave no task pending, so the sinks themselves are what
    this case reads.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    calls = _fail_second_consumer(monkeypatch)
    recorded = _record_captures(monkeypatch)

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
        assert len(recorded) == 1, (
            f"a failed setup must reclaim exactly once, got {len(recorded)} calls"
        )
        sinks = recorded[0]
        assert len(sinks) == 2, (
            f"both streams' accumulators must be handed over, got {sinks!r}"
        )
        assert all(sink is not None for sink in sinks), (
            f"the setup resolved both sinks before it failed, got {sinks!r}"
        )
        assert sinks[0] is not sinks[1], (
            "each stream must keep its own accumulator, not a shared one"
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
