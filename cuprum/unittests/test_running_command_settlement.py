"""What a handle's settlement owes the caller around the run.

The hooks an ``async with`` block registered are paired with its entry, and the
stamps on the lines it publishes are measured from the spawn rather than from
the machine's monotonic clock. Both are properties of the settlement rather
than of the child, so neither is reachable by asking a real child nicely.

What a deadline a caller names bounds, and what its expiry carries, is covered
in :mod:`cuprum.unittests.test_running_command_settlement_deadlines`; the
window in which a clock runs out after the child is already gone is covered in
:mod:`cuprum.unittests.test_running_command_settlement_windows`. The handle's
own bookkeeping lives in
:mod:`cuprum.unittests.test_running_command_lifecycle`,
and the refusals a delivery can meet in
:mod:`cuprum.unittests.test_running_command_signals`.
"""

from __future__ import annotations

import asyncio
import time
import typing as typ

import pytest

from cuprum import ScopeConfig, scoped, sh
from cuprum.events import TerminalOutcome
from cuprum.sh import RunOutputOptions
from cuprum.unittests._running_command_support import _observe, _settled_event
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.lines import LineEvent
    from cuprum.sh import CommandResult, SafeCmd


class _HookRefusalError(RuntimeError):
    """The failure a deliberately refusing ``after`` hook raises."""


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
