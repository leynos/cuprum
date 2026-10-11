"""Scaffolding for a cancellation landing between the exit and the gather.

A streamed run that owns its group has three awaits between the child's exit
and the completed gather of its stream consumers: stopping the idle heartbeat,
awaiting the stdin writer, and the gather itself. Each can be cancelled, and
each needs the same aftermath — the group is settled and the consumers are
reconciled. The modules that drive those waits need the same handful of tools:
a rendezvous that puts a cancellation exactly on an await, bounded waits that
report a hung run as the defect it is rather than as an infrastructure timeout,
and helpers that assert the descendant its child left behind is gone.

They live here, rather than in each test module, so all three can use one
definition. Nothing here decides *what* happens after a cancellation: the
boundary, the group settlement, and the consumer drain are all production code
the tests exercise unpatched. What these helpers replace is only the arrival
signal, so a cancellation lands on the await a test names however slowly the
machine runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, _subprocess_drain, _subprocess_stream_run, scoped
from cuprum.sh import (
    ExecutionContext,
    ProcessGroupPolicy,
    RunOutputOptions,
    StdinInput,
    TimeoutExpired,
)
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.timeouts import (
    descendant_then_exit_argv,
    process_is_running,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum._pipeline_types import _StageObservation
    from cuprum._subprocess_drain import _DrainContext, _RunTaskOwnership
    from cuprum._subprocess_execution import _SubprocessExecution
    from cuprum.sh import SafeCmd

# A run whose group was never settled does not return at all, so every wait
# here is bounded and a bound expiring is reported as the defect it is.
RUN_BUDGET_S = 20.0

# Enough to overrun a pipe buffer, so the child's stdin cannot be flushed until
# something reads it. The descendant never does, which keeps the writer parked
# for as long as a test needs it to be.
PARKING_STDIN_BYTES = 2 * 1024 * 1024


class Gate:
    """A rendezvous the test releases, so a cancel lands where it is aimed.

    Sleeping and cancelling would make these tests depend on how long an await
    happens to take. A gate replaces that with a handshake: the patched call
    announces it has been reached, the test cancels, and only then is the call
    allowed to finish. Nothing else waits on the release, so a cancellation
    issued after the announcement is delivered to the parked call and nowhere
    else.
    """

    def __init__(self) -> None:
        """Create both events unreached and unreleased."""
        self.reached = asyncio.Event()
        self.released = asyncio.Event()

    async def hold(self) -> None:
        """Announce arrival, then wait to be released."""
        self.reached.set()
        await self.released.wait()


def held_by(
    gate: Gate,
    call: cabc.Callable[..., cabc.Awaitable[object]],
) -> cabc.Callable[..., cabc.Awaitable[None]]:
    """Wrap *call* so it parks on *gate* before doing its real work.

    The wrapper is what makes a cancellation land on the await under test
    rather than on whatever the scheduler happened to be doing. Only arrival is
    intercepted: once released, *call* is the production one.

    Returns
    -------
    collections.abc.Callable[..., collections.abc.Awaitable[None]]
        An awaitable callable that announces its arrival at *gate*, waits to be
        released, and then delegates to *call*.
    """

    async def wrapper(*args: object) -> None:
        """Announce arrival, wait for release, then perform the real call."""
        await gate.hold()
        await call(*args)

    return wrapper


class TeardownWatch:
    """Instruments that watch a run's post-exit teardown half.

    A first cancellation has to enter the cleanup the ordinary way and a second
    has to arrive while the group is still being settled, which is what tells
    one shielded unit apart from two shielded calls in sequence. Three
    observations make that deterministic: the writer await announces the run
    has reached the boundary, the group teardown is held open at a gate, and
    the drain records that reconciliation was reached.

    Arming is part of construction. A watch that had to be installed in a
    second step would be one a test could forget, and the forgotten watch fails
    as a run that never reaches its boundary — a slow, misleading way to report
    a test defect. The originals are captured before the module attributes are
    rebound, because the wrappers have to call through to them.

    The drain is bound on ``cuprum._subprocess_drain``, not on the stream-run
    module. Nothing in the stream-run module calls ``_drain_stream_consumers``;
    reconciliation calls it, and reconciliation resolves the name in its own
    module's globals — so a rebound attribute on the importing module would
    watch a name no production path reads, and the drain would go unrecorded
    while the test reported a boundary that was never reached.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Capture the originals and rebind the module attributes to watch."""
        self.gate = Gate()
        self.entered_boundary = asyncio.Event()
        self.drained = asyncio.Event()
        self._await_writer = _subprocess_stream_run._await_stdin_writer
        self._settle_group = _subprocess_stream_run._settle_owned_group_before_draining
        self._drain = _subprocess_drain._drain_stream_consumers
        monkeypatch.setattr(
            _subprocess_stream_run,
            "_await_stdin_writer",
            self._watching_await,
        )
        monkeypatch.setattr(
            _subprocess_stream_run,
            "_settle_owned_group_before_draining",
            self._gated_settle,
        )
        monkeypatch.setattr(
            _subprocess_drain,
            "_drain_stream_consumers",
            self._recording_drain,
        )

    async def _watching_await(self, tasks: _RunTaskOwnership) -> None:
        """Announce the writer await, then perform it for real."""
        self.entered_boundary.set()
        await self._await_writer(tasks)

    async def _gated_settle(
        self,
        process: asyncio.subprocess.Process,
        execution: _SubprocessExecution,
    ) -> None:
        """Hold the group teardown until the test releases it."""
        await self.gate.hold()
        await self._settle_group(process, execution)

    async def _recording_drain(
        self,
        consumers: tuple[asyncio.Task[str | None], asyncio.Task[str | None]],
        context: _DrainContext,
    ) -> tuple[str | None, str | None]:
        """Run the real drain, recording that reconciliation was reached."""
        self.drained.set()
        return await self._drain(consumers, context)


class SpawnedWriter:
    """Instruments the stdin writer a run spawns, without replacing it.

    A test that has to look at the writer *while* the run is in flight needs a
    handle on the task, and the run builds it internally. Only the spawn is
    intercepted, so the task itself is the production one and the cancellation
    under test is the production cleanup's; the handle is an observation, not a
    substitute.

    Arming is part of construction, for the same reason it is on
    :class:`TeardownWatch`: a watch installed in a second step is one a test can
    forget, and the forgotten watch reports as a run that never spawned a
    writer rather than as the test defect it is.

    What this deliberately does *not* offer is a record of every writer a run
    ever spawned. A run spawns at most one, so a list would only invite
    assertions over it that read as "every task the run retained" while being
    satisfied by the group signal that releases the writer anyway. Use
    :attr:`task` for the precondition that the writer was live, and pin
    settlement where nothing else can stand in for it.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Rebind the spawn so the task it creates is retained here."""
        self.task: asyncio.Task[None] | None = None
        self._spawn = _subprocess_stream_run._spawn_stdin_writer
        monkeypatch.setattr(
            _subprocess_stream_run,
            "_spawn_stdin_writer",
            self._watching_spawn,
        )

    def _watching_spawn(
        self,
        process: asyncio.subprocess.Process,
        stdin_data: bytes | None,
        observation: _StageObservation,
    ) -> asyncio.Task[None] | None:
        """Spawn the writer unchanged, keeping a handle on the task it made."""
        self.task = self._spawn(process, stdin_data, observation)
        return self.task

    def assert_settled_after_run(self) -> None:
        """Assert a writer was spawned, and that the run settled it before returning.

        Read this only where no group signal can stand in for the settlement:
        settling a group closes the pipes a parked writer waits on, which
        releases it whether or not reconciliation ever reached it. Under
        ``OWN_GROUP`` that makes both halves of this pass on a run whose cleanup
        never touched the writer at all.
        """
        assert self.task is not None, (
            "the run spawned no stdin writer, so the payload was never handed "
            "to a task and the parked-writer case was not reached"
        )
        assert self.task.done(), (
            "the run returned with its stdin writer still running, so the "
            "expiry's cleanup stopped waiting on the writer without ever "
            "settling it"
        )


def recorded_descendant(pid_file: Path) -> int | None:
    """Read the descendant pid the child recorded, if it recorded one.

    Returns
    -------
    int | None
        The descendant's pid, or ``None`` when the child never wrote the file
        or wrote something that is not an integer. Both mean the same thing to
        a test — this run never reached the state under test — so a partial or
        corrupted write is reported the same way as an absent one.
    """
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def reap_descendant(pid_file: Path, descendant_pid: int | None) -> None:
    """Force-kill the descendant so a failing test does not leak it."""
    if descendant_pid is None:
        descendant_pid = recorded_descendant(pid_file)
    if descendant_pid is not None and process_is_running(descendant_pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(descendant_pid, signal.SIGKILL)


async def await_within(awaitable: cabc.Awaitable[object], *, context: str) -> None:
    """Fail the test rather than hang when *awaitable* never completes."""
    try:
        async with asyncio.timeout(RUN_BUDGET_S):
            await awaitable
    except TimeoutError:
        pytest.fail(
            f"the run never reached {context} within {RUN_BUDGET_S}s, so the "
            "cancellation this test aims at that await could not be delivered"
        )


async def settle(run_task: asyncio.Task[object]) -> None:
    """Await a cancelled run, asserting it ends by cancellation and in time."""
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(run_task, RUN_BUDGET_S)


async def assert_reclaimed(descendant_pid: int, message: str) -> None:
    """Assert the run reached the descendant its child left holding the pipes."""
    await asyncio.to_thread(
        wait_for_process_death,
        descendant_pid,
        context="a cancelled run's post-exit cleanup",
    )
    assert not process_is_running(descendant_pid), message


async def run_to_its_own_deadline(
    run: cabc.Coroutine[object, object, object],
) -> TimeoutExpired:
    """Await *run*, requiring it to end on its own deadline and in time.

    The run is given the deadline under test *and* an outer bound, and the two
    are different claims: the outer bound reports a run that never ended at all
    as the defect it is rather than as an infrastructure timeout, and the bound
    on the elapsed time is what shows the run ended because of *its* deadline
    rather than because the outer one gave up on it. Both are asserted here so
    a test cannot accidentally check one and read it as the other.

    Returns
    -------
    TimeoutExpired
        The exception the run raised, so a caller can assert on the payload the
        expiry carried — the drained output, for instance — rather than only on
        the fact that it raised.
    """
    started = time.monotonic()
    with pytest.raises(TimeoutExpired) as raised:
        await asyncio.wait_for(run, RUN_BUDGET_S)
    elapsed = time.monotonic() - started
    assert elapsed < RUN_BUDGET_S, (
        "the run reached this assertion too late to have been bounded by its "
        "own deadline rather than by the test's outer bound"
    )
    return raised.value


def recorded_descendant_or_fail(pid_file: Path) -> int:
    """Return the descendant's pid, failing the test when none was recorded."""
    descendant_pid = recorded_descendant(pid_file)
    assert descendant_pid is not None, (
        "the child never recorded the descendant it left behind, so this run "
        "did not reach the state under test"
    )
    return descendant_pid


def start_owned_run(
    builder: cabc.Callable[..., SafeCmd],
    pid_file: Path,
    *,
    stdin: StdinInput | None = None,
) -> asyncio.Task[object]:
    """Start an owned run of a child that exits leaving a descendant behind."""
    command = builder(*descendant_then_exit_argv(pid_file, exit_code=0))
    return asyncio.ensure_future(
        command.run(
            output=RunOutputOptions(capture=True, echo=False),
            stdin=stdin,
            context=ExecutionContext(process_group=ProcessGroupPolicy.OWN_GROUP),
        ),
    )


async def reach_gate_and_cancel(
    run_task: asyncio.Task[object],
    gate: Gate,
    *,
    context: str,
) -> None:
    """Cancel the run exactly while a gated await holds it open.

    The order matters and is the whole reason this is a helper: the test waits
    for the run to announce it has arrived, cancels while it is parked there,
    and only then releases the gate. Releasing first would let the await finish
    and the cancellation land somewhere else entirely.
    """
    await await_within(gate.reached.wait(), context=context)
    run_task.cancel()
    gate.released.set()
    await settle(run_task)


@pytest.fixture
def owned_builder() -> cabc.Iterator[cabc.Callable[..., SafeCmd]]:
    """Allowlist the interpreter and yield a builder for it, for one test.

    The allowlist and the builder are one thing here rather than two fixtures
    because they have to agree: a builder bound to a catalogue the open scope
    does not cover produces a run that is refused before it spawns, and every
    test in these modules needs exactly one of each. Asking for them separately
    left the agreement to the caller's discipline and made it the largest part
    of each signature.

    The scope is entered around the whole test, not around the part that
    happens to spawn, because a run is only permitted to spawn inside it and
    these runs outlive the statement that starts them.

    Yields
    ------
    collections.abc.Callable[..., SafeCmd]
        A builder for the current interpreter, usable for as long as the test
        is running because the scope that permits it stays open that long.
    """
    env = build_python_catalogue_env()
    with scoped(ScopeConfig(allowlist=frozenset({env.program}))):
        yield env.builder
