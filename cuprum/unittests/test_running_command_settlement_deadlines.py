"""What a deadline a caller names bounds, and what its expiry carries.

:mod:`cuprum.unittests.test_running_command_lifecycle` covers the handle's
bookkeeping — repeated stops and a cancellation landing mid-grace — and
:mod:`cuprum.unittests.test_running_command_signals` covers the refusals a
delivery can meet. This module covers the property those cases lean on: the
settlement is shared, so a deadline one caller names must bound *that caller*
and nobody else.

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

What a deadline does once the child is already gone, and what it must *not*
record, is covered in
:mod:`cuprum.unittests.test_running_command_settlement_windows`; the remaining
settlement cases are in :mod:`cuprum.unittests.test_running_command_settlement`.

Two cases drive process doubles, because the branch under test — a caller's
clock running out while the child is still healthy — cannot be asked of a real
child deterministically. The rest use real children, because what they assert
is produced by the stream plumbing rather than by the handle, and a double
would only re-state the double's own arrangement.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.events import TerminalOutcome
from cuprum.sh import RunOutputOptions, TimeoutExpired
from cuprum.unittests._running_command_support import (
    _TERMINATED,
    _handle,
    _observe,
    _settled_event,
    _StubProcess,
)
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import CHILD_STDERR, CHILD_STDOUT

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent
    from cuprum.program import Program
    from cuprum.sh import SafeCmd

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
