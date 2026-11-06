"""A failed streamed setup must hand its capture sinks to the reclaim.

The streamed run resolves both streams' capture accumulators before it starts
the consumers, and a consumer that fails to start leaves those accumulators
holding spill files. The reclaim removes them, but only if the run passes them
on: the helper cannot discover a sink the failed setup never handed over.

The existing setup-failure case proves the child and the started consumer are
reclaimed. It cannot prove the sinks arrived, because a run that dropped them
would still reap the child and leave no task pending — the spill file would
simply survive. This case watches the hand-off itself, at the seam between the
run and the helper, and asserts that the two sinks are the accumulators the run
resolved and that the reclaim cleaned each of them exactly once.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_stream_run, _subprocess_streams, scoped, sh
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

    from cuprum._capture_types import CaptureFinal, CaptureSink
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum._subprocess_streams import _StreamSpawnInputs
    from cuprum.catalogue import ProgramCatalogue
    from cuprum.events import ExecEvent


def _command(*argv: str) -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a Python command and the allowlist catalogue that admits it."""
    program, catalogue = _python_catalogue("streamed-setup-captures")
    return sh.make(program, catalogue=catalogue)(*argv), catalogue


class _RecordingSink:
    """A transparent accumulator wrapper that counts the cleanups it receives.

    The reclaim receives this wrapper in place of the real accumulator, so the
    case can prove both that the run handed over the accumulator it resolved
    and that the reclaim asked it to release its spill file. Every other
    operation is delegated unchanged, so the wrapper is indistinguishable from
    the sink it hides except for the count it keeps.
    """

    def __init__(self, sink: CaptureSink) -> None:
        """Bind the accumulator this wrapper stands in for."""
        self.sink = sink
        self.cleanups = 0

    async def absorb(self, chunk: bytes) -> None:
        """Take one raw chunk, as the wrapped accumulator would."""
        await self.sink.absorb(chunk)

    async def finalize(self) -> CaptureFinal:
        """Return the wrapped accumulator's finalized output."""
        return await self.sink.finalize()

    async def cleanup(self) -> None:
        """Record the cleanup, then let the wrapped accumulator release."""
        self.cleanups += 1
        await self.sink.cleanup()


def _record_resolved_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[CaptureSink | None, ...]]:
    """Record the accumulators the streamed run resolved for its consumers.

    Returns
    -------
    list[tuple[CaptureSink | None, ...]]
        One entry per stream-prep call, holding the sinks that call resolved.
    """
    prepare = typ.cast(
        "cabc.Callable[..., _StreamSpawnInputs]",
        _subprocess_streams._prepare_stream_spawn,
    )
    resolved: list[tuple[CaptureSink | None, ...]] = []

    def recording(*args: object, **kwargs: object) -> _StreamSpawnInputs:
        """Record the resolved sinks, then return the real spawn inputs."""
        spawn = prepare(*args, **kwargs)
        resolved.append(tuple(spawn.spawn_context.captures))
        return spawn

    monkeypatch.setattr(_subprocess_streams, "_prepare_stream_spawn", recording)
    return resolved


def _record_captures(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[CaptureSink | None, ...]]:
    """Record the capture sinks each reclaim is handed, then reclaim as usual.

    Each sink is wrapped so the case can assert that the reclaim cleaned it,
    not merely that it was passed along.

    Returns
    -------
    list[tuple[CaptureSink | None, ...]]
        One entry per reclaim call, holding the wrappers that call received.
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
        wrapped = tuple(
            None if sink is None else _RecordingSink(sink) for sink in captures
        )
        recorded.append(wrapped)
        await original(process, execution, tasks, captures=wrapped)

    monkeypatch.setattr(
        _subprocess_stream_run,
        "_abandon_started_process",
        recording,
    )
    return recorded


def _assert_handed_over_the_resolved_accumulators(
    sinks: tuple[CaptureSink | None, ...],
    resolved: list[tuple[CaptureSink | None, ...]],
) -> None:
    """Assert the reclaim received the run's own accumulators, each cleaned once.

    Identity separates a reclaim that was handed the run's own spill state from
    one that accepted some other pair; the count separates a reclaim that
    cleaned each accumulator from one that only received it.
    """
    assert len(resolved) == 1, (
        f"the streamed setup must resolve one stream pair, got {len(resolved)}"
    )
    wrappers = [typ.cast("_RecordingSink", sink) for sink in sinks]
    for wrapper, expected in zip(wrappers, resolved[0], strict=True):
        assert expected is not None, (
            "the setup resolved a sink for each stream before it failed"
        )
        assert wrapper.sink is expected, (
            "the reclaim must receive the accumulator the run resolved, "
            f"got {wrapper.sink!r} instead of {expected!r}"
        )
        assert wrapper.cleanups == 1, (
            "the reclaim must clean each handed-over accumulator exactly "
            f"once, got {wrapper.cleanups} for {expected!r}"
        )


def test_streamed_setup_failure_hands_both_capture_sinks_to_the_reclaim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stream_backend: str,
) -> None:
    """Both resolved accumulators must reach the reclaim, not just the child.

    The failure lands on the second consumer, which is after both stream
    configs were resolved. A run that passed an empty ``captures`` would still
    reap the child and leave no task pending, so the sinks themselves are what
    this case reads. Identity and the cleanup count separate a reclaim that
    received the run's own accumulators and released them from one that
    accepted some other pair and left the spill files behind.
    """
    command, catalogue = _command(*child_argv(tmp_path / "child.pid"))
    events: list[ExecEvent] = []
    calls = _fail_second_consumer(monkeypatch)
    resolved = _record_resolved_captures(monkeypatch)
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
        _assert_handed_over_the_resolved_accumulators(sinks, resolved)
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
