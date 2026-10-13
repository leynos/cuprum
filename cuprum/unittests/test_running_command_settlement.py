"""What one handle owes several callers with their own deadlines.

:mod:`cuprum.unittests.test_running_command_lifecycle` covers the handle's
bookkeeping — repeated stops and a cancellation landing mid-grace — and
:mod:`cuprum.unittests.test_running_command_signals` covers the refusals a
delivery can meet. This module covers the property those cases lean on: the
settlement is shared, so a deadline one caller names must bound *that caller*
and nobody else. All three drive the same process doubles, which live in
:mod:`cuprum.unittests._running_command_support`.

Three consequences are asserted here and nowhere else.

The first is that a deadline expires the caller that named it rather than the
handle. An implementation driving the shared settlement under one caller's
clock imposes that clock on every caller: a wait that named no deadline is cut
short by a stranger's, and the child is stopped to serve a clock that was never
that caller's.

The second is that the expiry still carries what the child said. A deadline is
a report about the caller's clock, not a licence to discard output the run had
already drained — the contract the streamed ``run()`` path already keeps.

The third is that a deadline-ended run settles as a *timeout*. The child is
signalled to make the deadline happen, so the exit code recorded is ``-15``; a
terminal outcome derived from that code alone would publish an ordinary
non-zero exit for a run that was simply cut short.

Two cases drive process doubles, because the branch under test — a caller's
clock running out while the child is still healthy — cannot be asked of a real
child deterministically. The rest use real children, because what they assert
is produced by the stream plumbing rather than by the handle, and a double
would only re-state the double's own arrangement.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum._process_handle import settlement
from cuprum.events import ExecEvent, TerminalOutcome
from cuprum.sh import RunOutputOptions, TimeoutExpired
from cuprum.unittests._running_command_support import (
    _probe_of,
    _TERMINATED,
    _handle,
    _StubProcess,
    _StubRunConfig,
)
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import CHILD_STDERR, CHILD_STDOUT

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineEvent
    from cuprum.program import Program
    from cuprum.sh import CommandResult, SafeCmd

#: How long a deadline is given to expire. Long enough that a scheduler under
#: load cannot expire the wait before the case has set it running, short enough
#: that every case stays well inside the per-test timeout.
_DEADLINE = 0.2

#: How long a case waits for the child it starts to be heard from. The child
#: writes immediately, so this is a guard against a hang rather than a race
#: the case expects to lose.
_READY = 10.0

#: How long a waiter that should never raise is allowed to take. It settles as
#: soon as the child it is waiting on is stopped, so this too is only a guard.
_SETTLED = 5.0

#: How long the double's drain is held open, so a deadline can fire inside the
#: window that follows the exit wait. Comfortably longer than :data:`_DEADLINE`
#: plus the capture EOF grace the drain grants its consumers, and far shorter
#: than the per-test timeout.
_DRAIN_DELAY = 1.0

#: How long a case holds a reaper that has not published yet. Longer than
#: :data:`_DEADLINE` by enough that the clock is certain to run out while the
#: publication is outstanding, and far shorter than the guard the case waits
#: under.
_PUBLICATION_DELAY = 0.5


class _HookRefusalError(RuntimeError):
    """The failure a deliberately refusing ``after`` hook raises."""


#: A child that writes its error marker first, then its output marker, then
#: blocks far longer than any case here will wait. The order is load-bearing:
#: a case waits for the *stdout* line to arrive, which therefore guarantees
#: both streams had already been written and flushed by the time it acts. That
#: guarantee rests on the wait, not on the child's ordering: the callback must
#: ignore stderr, because the child writes that first and a callback that fired
#: on it would report readiness while the stdout marker did not yet exist.
_CHILD_SOURCE = (
    "import sys, time\n"
    f"sys.stderr.write({CHILD_STDERR!r} + chr(10))\n"
    "sys.stderr.flush()\n"
    f"sys.stdout.write({CHILD_STDOUT!r} + chr(10))\n"
    "sys.stdout.flush()\n"
    "time.sleep(300)\n"
)


def _hear_stdout(event: LineEvent, heard: asyncio.Event) -> None:
    """Set *heard* once the child's stdout line arrives, ignoring stderr.

    The child writes stderr first, so a callback that fired on any line would
    report readiness while the stdout marker was still unwritten — and the
    deadline could then stop the child before the output the case is about
    ever existed, leaving the assertion to fail intermittently under load.
    """
    if event.stream == "stdout":
        heard.set()


def _observe() -> tuple[list[ExecEvent], cabc.Callable[[ExecEvent], None]]:
    """Return an event log and the observer that fills it."""
    events: list[ExecEvent] = []

    def record(event: ExecEvent) -> None:
        """Record each lifecycle event the run publishes."""
        events.append(event)

    return events, record


def _settled_event(events: list[ExecEvent]) -> ExecEvent:
    """Return the run's terminal event, asserting it settled the run.

    Returns
    -------
    ExecEvent
        The run's ``settled`` event, whose ``terminal_outcome`` is what the
        deadline case asserts on.
    """
    assert events, "the run must publish events for this case to read"
    settled = events[-1]
    assert settled.phase == "settled", (
        f"the terminal event must settle the run, got {settled.phase}"
    )
    return settled


def _blocking_command() -> tuple[SafeCmd, Program]:
    """Build an allowlisted child that writes on both streams, then blocks.

    Returns
    -------
    tuple[SafeCmd, Program]
        The command, and the program the scope must allowlist for it.
    """
    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    return builder("-c", _CHILD_SOURCE), python_program


def test_a_deadline_bounds_its_own_caller_and_no_other() -> None:
    """One wait expires under a deadline while the wait beside it stays open.

    The two waits share one settlement, which is what makes a deadline a
    property of the caller rather than of the handle. A settlement driven under
    one caller's clock hands its expiry to every waiter, so the caller that
    named no deadline is told a child it still holds ran out of time — and the
    child is stopped to serve a clock that was never that caller's.

    The unbounded wait is started first, so the bounded one joins a run already
    in flight rather than starting one; that is the ordering in which a
    settlement that had adopted the deadline it was first handed would have
    nothing to adopt and the reverse in which it would adopt the wrong one.
    """

    async def run_case() -> None:
        """Start an unbounded wait, then expire a bounded one beside it."""
        process = _StubProcess()
        handle = _handle(process)
        unbounded = asyncio.ensure_future(handle.wait())
        # Let the unbounded waiter reach the shared settlement before the
        # bounded one arrives, so neither creates it by accident.
        for _ in range(3):
            await asyncio.sleep(0)

        with pytest.raises(TimeoutExpired) as raised:
            await handle.wait(timeout=_DEADLINE)

        assert raised.value.timeout == _DEADLINE, (
            f"the expiry must name its own caller's deadline {_DEADLINE}, got "
            f"{raised.value.timeout}"
        )
        assert process.terminate_calls == 1, (
            "the expiry must stop the child it gave up on, got "
            f"{process.terminate_calls} stops"
        )

        result = await asyncio.wait_for(unbounded, timeout=_SETTLED)
        assert result.exit_code == _TERMINATED, (
            "the waiter that named no deadline must receive the recorded "
            f"outcome rather than an expiry, got {result.exit_code}"
        )

    asyncio.run(run_case())


def test_a_spent_deadline_is_not_re_raised_for_a_later_caller() -> None:
    """A deadline raises once; later callers read the outcome it recorded.

    The stop belongs to the handle rather than to the caller that asked for it,
    so the outcome it produced is what every later operation reads. A second
    bounded wait must return it rather than expire again — and must not signal
    a child that is already gone while doing so.
    """

    async def run_case() -> None:
        """Expire a wait, then read the outcome through two later calls."""
        process = _StubProcess()
        handle = _handle(process)

        with pytest.raises(TimeoutExpired):
            await handle.wait(timeout=_DEADLINE)

        recorded = handle.result
        assert recorded is not None, (
            "the stopped child's outcome must be recorded, not discarded"
        )
        assert await handle.wait(timeout=_DEADLINE) is recorded, (
            "a later bounded wait must report the recorded outcome rather than "
            "re-arm a deadline that is already spent"
        )
        assert process.terminate_calls == 1, (
            f"the child must be stopped once, got {process.terminate_calls} stops"
        )
        assert await handle.terminate() is recorded, (
            "a stop after a spent deadline must report the recorded outcome"
        )

    asyncio.run(run_case())


def test_an_expiry_carries_the_output_the_child_had_written() -> None:
    """The expiry reports what the child wrote before it was stopped.

    A deadline says the caller's clock ran out; it says nothing about the
    child's output, which the settlement drains exactly once as the stop
    completes. An expiry reporting ``None`` would tell a caller nothing about a
    child that had, in fact, said something first — the contract the streamed
    ``run()`` path already keeps.
    """

    async def run_case() -> None:
        """Expire a real child that has already written on both streams."""
        command, python_program = _blocking_command()
        heard = asyncio.Event()

        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            handle = await command.start(
                output=RunOutputOptions(
                    on_line=lambda event: _hear_stdout(event, heard),
                ),
            )
            # The child writes stderr first, so hearing its stdout means both
            # streams have been written and the deadline cannot cut either off
            # before it exists.
            await asyncio.wait_for(heard.wait(), timeout=_READY)
            with pytest.raises(TimeoutExpired) as raised:
                await handle.wait(timeout=_DEADLINE)

        expiry = raised.value
        assert isinstance(expiry.output, str), (
            "the expiry must carry the output captured before the deadline, "
            f"decoded rather than raw, got {expiry.output!r}"
        )
        assert CHILD_STDOUT in expiry.output, (
            f"the child's stdout marker must survive the stop, got {expiry.output!r}"
        )
        assert isinstance(expiry.stderr, str), (
            "the expiry must carry the child's error output too, decoded "
            f"rather than raw, got {expiry.stderr!r}"
        )
        assert CHILD_STDERR in expiry.stderr, (
            f"the child's stderr marker must survive the stop, got {expiry.stderr!r}"
        )

    asyncio.run(run_case())


def test_an_expired_run_settles_as_a_timeout() -> None:
    """A deadline-ended run reports ``TIMEOUT``, not the signal that ended it.

    The child is stopped to make the deadline happen, so the exit code the
    settlement records is ``-15`` — the signal, not the reason. A terminal
    outcome derived from that code alone would tell a consumer the command
    failed on its own, which is exactly the distinction between a command that
    timed out and one that ended badly.
    """

    async def run_case() -> None:
        """Expire a handle's wait and read the run's terminal event."""
        command, python_program = _blocking_command()
        events, observe = _observe()
        heard = asyncio.Event()

        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(observe),
        ):
            handle = await command.start(
                output=RunOutputOptions(
                    on_line=lambda event: _hear_stdout(event, heard),
                ),
            )
            await asyncio.wait_for(heard.wait(), timeout=_READY)
            with pytest.raises(TimeoutExpired):
                await handle.wait(timeout=_DEADLINE)

        settled = _settled_event(events)
        assert settled.terminal_outcome is TerminalOutcome.TIMEOUT, (
            "a deadline-ended run must settle as a timeout, got "
            f"{settled.terminal_outcome}"
        )
        assert settled.exit_code == _TERMINATED, (
            "the event must still report the signal that ended the child, got "
            f"{settled.exit_code}"
        )

    asyncio.run(run_case())


def test_a_settled_handle_runs_the_callers_after_hooks() -> None:
    """The handle pairs its ``before`` hooks with the caller's ``after`` hooks.

    ``_prepare_owned_command`` runs ``before`` hooks on the way in, so a caller
    that registers both for auditing gets one half of the pair unless the
    settlement runs the other. The hook receives the same ``CommandResult`` the
    caller's own ``wait`` returns, because it is the run's outcome rather than a
    summary of it.
    """
    seen: list[tuple[SafeCmd, CommandResult]] = []
    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    command = builder("-c", "print('hooked', flush=True)")

    async def run_case() -> CommandResult:
        """Start a child, let it exit, and settle the handle."""
        with scoped(
            ScopeConfig(
                allowlist=frozenset([python_program]),
                after_hooks=(lambda cmd, result: seen.append((cmd, result)),),
            ),
        ):
            handle = await command.start()
            return await handle.wait()

    result = asyncio.run(run_case())

    assert len(seen) == 1, (
        f"a settled handle must run the caller's after hook exactly once, got "
        f"{len(seen)} calls"
    )
    hooked_cmd, hooked_result = seen[0]
    assert hooked_cmd is command, (
        "the hook must receive the command the handle was started from, got "
        f"{hooked_cmd!r}"
    )
    assert hooked_result is result, (
        "the hook must receive the settled outcome rather than a copy of it, "
        f"got {hooked_result!r}"
    )


def test_a_refusing_after_hook_settles_the_run_as_its_error() -> None:
    """A hook that raises is the run's terminal error, not a swallowed one.

    The hooks run before the run is finalized, which is what makes this true:
    finalizing first would have committed a success and then let a failure
    arrive afterwards, leaving the run published as completed while its own
    caller saw a traceback. The classification the caller's hook uses is the
    same one ``run()`` reports for a failing hook.
    """
    events, observe = _observe()

    def refuse(_cmd: SafeCmd, _result: CommandResult) -> None:
        """Refuse the outcome the way an auditing hook with a bug would."""
        raise _HookRefusalError

    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    command = builder("-c", "print('hooked', flush=True)")

    async def run_case() -> None:
        """Start a child, let it exit, and settle under the refusing hook."""
        with (
            scoped(
                ScopeConfig(
                    allowlist=frozenset([python_program]),
                    after_hooks=(refuse,),
                ),
            ),
            sh.observe(observe),
        ):
            handle = await command.start()
            with pytest.raises(_HookRefusalError):
                await handle.wait()

    asyncio.run(run_case())

    settled = _settled_event(events)
    assert settled.terminal_outcome is TerminalOutcome.ERROR, (
        "a run refused by its own after hook must settle as an error, got "
        f"{settled.terminal_outcome}"
    )


def test_a_deadline_after_the_child_exited_is_not_recorded() -> None:
    """A clock that fires once the child is gone reports the child's own exit.

    The settlement outlives the child by the drain: after the exit wait
    returns, the captured consumers are given an EOF grace to observe EOF, and
    a grandchild holding an inherited pipe can extend that further. A deadline
    landing in that window has nothing left to interrupt — the child is
    already gone — so recording it would publish ``TIMEOUT`` for a run that
    ended on its own, and skip its ``after`` hooks while doing so.

    The double exits before its ``wait()`` returns, which is that window: the
    exit has happened and the settlement is still running. Both halves of the
    rule are asserted here, because they fail together: the caller reads the
    child's status rather than an expiry, and the hook the run registered — the
    one a deadline branch does not run — was run.
    """
    hook_calls: list[int] = []

    class _ExitedProcess(_StubProcess):
        """A double that has already exited before anything waits on it."""

        def __init__(self, *, pid: int = 4399) -> None:
            super().__init__(pid=pid)
            self.exit_with(0)

    async def run_case() -> CommandResult:
        """Wait with a deadline that fires while the exit is being drained."""
        process = _ExitedProcess()
        handle = _handle(
            process,
            _StubRunConfig(
                after_hooks=(lambda _cmd, result: hook_calls.append(result.exit_code),),
                # The settlement's drain stays open past the deadline, which is
                # the window the rule under test is about.
                drain_delay=_DRAIN_DELAY,
            ),
        )
        return await handle.wait(timeout=_DEADLINE)

    result = asyncio.run(run_case())

    assert result.exit_code == 0, (
        "a child that exited on its own must report its own status, got "
        f"{result.exit_code}"
    )
    assert hook_calls == [0], (
        "the run ended on its own, so its after hook must have run with the "
        f"child's own outcome; got calls {hook_calls!r}, which is empty when "
        "the deadline was wrongly recorded and took the timeout branch"
    )


def test_a_deadline_reads_the_kernel_not_the_published_code() -> None:
    """A child the kernel reports as exited is seen as exited before its reap.

    The case above covers a double whose ``wait()`` publishes the exit on its
    way back, so ``returncode`` is already set when the deadline is asked. On
    the ``wait4`` backend the real reaper publishes from an executor thread,
    which leaves a window a double of that shape cannot express: the child has
    genuinely finished, its status is genuinely a zombie's, and ``returncode``
    is still ``None`` because no worker has run the collection yet.

    A deadline landing in that window must still read the child as gone. The
    status is observable without collecting it — that is what the non-reaping
    probe is for — and reading the attribute instead records a timeout, signals
    a child that is already dead, and raises an expiry for a command that met
    its deadline. The double therefore holds the publication open for longer
    than the deadline: the case is about the reading the branch takes while the
    two disagree, and a publication that landed first would answer it for both.
    """

    hook_calls: list[int] = []

    class _UnreapedExitedProcess(_StubProcess):
        """A double the kernel reports as exited, with nothing published yet."""

        def __init__(self, *, pid: int = 4398) -> None:
            """Start ended on the kernel's view alone, and stay unpublished."""
            super().__init__(pid=pid)
            # ``exit_with`` is deliberately not called: it publishes the code,
            # and withholding that is the whole point. The double has no real
            # PID, so the kernel question the production probe would ask of a
            # real child is one only the double can answer — hence
            # ``kernel_exit_code`` and the patched probe below.
            self.kernel_exit_code: int | None = 0
            self.exited.set()
            self.collection_started = asyncio.Event()

        async def wait(self) -> int:
            """Reap the double, publishing only once the case lets it.

            The reaper is what turns the kernel's answer into the published
            code, and here it is held open so the two disagree across the
            deadline's evaluation rather than only within it. Nothing publishes
            the code before this returns, so a branch reading ``returncode``
            finds ``None`` for a child the probe can already see has ended.

            The publication waits on a clock rather than on the loop's
            scheduling, because it does not matter how many times the deadline
            branch is entered: what matters is that the attribute is still
            unset every time it is read. Announcing the entry first is what
            stops the case from passing on a reaper that never ran at all.
            """
            await self.exited.wait()
            self.collection_started.set()
            await asyncio.sleep(_PUBLICATION_DELAY)
            self.returncode = self.kernel_exit_code
            return self.returncode if self.returncode is not None else -1

    async def run_case() -> CommandResult:
        """Wait with a deadline that fires while the exit is uncollected."""
        process = _UnreapedExitedProcess()
        handle = _handle(
            process,
            _StubRunConfig(
                after_hooks=(lambda _cmd, result: hook_calls.append(result.exit_code),),
            ),
        )
        # The probe is the seam the double stands in for: a real child has a
        # PID the kernel can be asked about, and this one does not, so the
        # double supplies that answer rather than inventing a PID. A double
        # that answers ``None`` would make the case vacuous, because a branch
        # falling back to the attribute would agree with it.
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(settlement, "_observed_exit_code", _probe_of(process))
            result = await handle.wait(timeout=_DEADLINE)
        assert process.collection_started.is_set(), (
            "the double never collected the child, so no branch was ever asked "
            "what the unpublished attribute said"
        )
        assert process.returncode == 0, (
            "the double never published the exit, so the case did not exercise "
            "a child that ended while its code was still uncollected"
        )
        return result

    result = asyncio.run(run_case())

    assert result.exit_code == 0, (
        "a child the kernel reports as exited must report its own status, got "
        f"{result.exit_code}; a deadline recorded instead means the child was "
        "read through ``returncode`` rather than through the kernel"
    )
    assert hook_calls == [0], (
        "the run ended on its own, so its after hook must have run with the "
        f"child's own outcome; got calls {hook_calls!r}, which is empty when "
        "the deadline was wrongly recorded and took the timeout branch"
    )


def test_line_stamps_are_measured_from_the_spawn() -> None:
    """Every line's ``at`` is seconds since the command started, not uptime.

    The start instant travels on the bundle the child is spawned with, because
    the stream callbacks read it from there rather than from the record the
    handle holds. Left at its zero default, each stamp would be read from the
    machine's monotonic clock instead — a value in the millions of seconds, so
    a caller comparing two events could not tell a command that had just
    started from one that had been running for a very long time.
    """
    seen: list[LineEvent] = []
    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    command = builder("-c", "print('stamped line', flush=True)")

    async def run_case() -> None:
        """Start a child, hear its line, and let it exit."""
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            handle = await command.start(
                output=RunOutputOptions(on_line=seen.append),
            )
            await handle.wait()

    # Bounding the stamps by the case's own duration is a tighter and more
    # honest check than any fixed ceiling: the spawn happens inside the window
    # this span measures, so no honest stamp can exceed it.
    before = time.perf_counter()
    asyncio.run(run_case())
    elapsed = time.perf_counter() - before

    assert seen, "the child's output must reach the registered line hook"
    stamps = [event.at for event in seen]
    assert all(stamp >= 0.0 for stamp in stamps), (
        f"a line stamp is a duration and cannot be negative, got {stamps!r}"
    )
    assert all(stamp <= elapsed for stamp in stamps), (
        "every stamp is measured from the spawn, so none can exceed the time "
        f"this case took ({elapsed:.3f}s); a larger value means the stream "
        f"callbacks never received the spawn's start instant, got {stamps!r}"
    )
