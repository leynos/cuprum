"""The handle's start seam, and what it owes when starting fails.

``SafeCmd.start()`` is two steps that must not be allowed to drift apart: a
command is prepared — its presentation-sink session opened, its ``plan`` event
emitted, its ``before`` hooks run — and only then is a child spawned. Between
them sits a window in which preparation has succeeded and the child does not
exist yet.

The handle owns a spawned child completely, and that ownership is asserted
where the child is spawned. These cases cover the other half: failures inside
that window, and failures while the child's tasks are being attached. Both are
failures a caller cannot arrange on demand and both would otherwise leave the
handle's framing open behind a raised exception, so they are driven here with a
deliberately failing seam rather than with a real child.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, ScopeConfig, StdioTarget, scoped, sh
from cuprum._process_handle import tasks as handle_tasks
from cuprum.events import ExecEvent, TerminalOutcome
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum.program import Program
    from cuprum.sh import SafeCmd

#: The failure a seam pumps into ``start()``.
_START_FAILURE = "start seam failed"


def _observations() -> tuple[list[ExecEvent], cabc.Callable[[ExecEvent], None]]:
    """Return an event log and the observer that fills it."""
    events: list[ExecEvent] = []

    def observe(event: ExecEvent) -> None:
        """Record each lifecycle event the run publishes."""
        events.append(event)

    return events, observe


def _python_command() -> tuple[SafeCmd, Program]:
    """Build an allowlisted command that would succeed if it were spawned."""
    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    return builder("-c", "print('never reached')"), python_program


def _phases(events: list[ExecEvent]) -> list[str]:
    """Return the phases recorded, in order."""
    return [event.phase for event in events]


async def _start_with_failing_spawn(
    command: SafeCmd,
    python_program: Program,
) -> list[ExecEvent]:
    """Run ``start()`` where the spawn itself raises."""
    events, observe = _observations()

    # Async because it stands in for an ``async def`` seam the caller awaits;
    # there is nothing here to await before the failure.
    async def failing_spawn(  # ruff: ignore[unused-async] - replaces an async seam
        execution: _SubprocessExecution,
    ) -> typ.NoReturn:
        """Stand in for the spawn and fail.

        Raises
        ------
        RuntimeError
            Always, standing in for whatever a real spawn failure would raise.
        """
        _ = execution
        raise RuntimeError(_START_FAILURE)

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(observe),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(handle_tasks, "_spawn_subprocess", failing_spawn)
        with pytest.raises(RuntimeError, match=_START_FAILURE):
            await command.start()

    return events


async def _start_with_failing_attachment(
    command: SafeCmd,
    python_program: Program,
) -> list[ExecEvent]:
    """Run ``start()`` where attaching the run's tasks raises.

    The child is spawned for real and then abandoned by the seam, so this case
    also witnesses the reclaim: a handle that let the failure out without
    terminating the child would leave a live process behind with no owner.

    Returns
    -------
    list[ExecEvent]
        The events the run published, in order.
    """
    events, observe = _observations()
    spawned: list[asyncio.subprocess.Process] = []
    real_spawn = handle_tasks._spawn_subprocess

    async def recording_spawn(
        execution: _SubprocessExecution,
    ) -> asyncio.subprocess.Process:
        """Spawn for real, remembering the child the seam then abandons.

        Returns
        -------
        asyncio.subprocess.Process
            The child the real spawn produced, also appended to ``spawned``.
        """
        process = await real_spawn(execution)
        spawned.append(process)
        return process

    # Async for the same reason as the spawn double above: the seam it
    # replaces is awaited, and the failure happens before any await.
    async def failing_attach(  # ruff: ignore[unused-async] - replaces an async seam
        process: asyncio.subprocess.Process,
        execution: _SubprocessExecution,
    ) -> typ.NoReturn:
        """Stand in for task attachment and fail.

        Raises
        ------
        RuntimeError
            Always, after the child has been spawned for real.
        """
        _ = process, execution
        raise RuntimeError(_START_FAILURE)

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(observe),
        pytest.MonkeyPatch.context() as patch,
    ):
        patch.setattr(handle_tasks, "_spawn_subprocess", recording_spawn)
        patch.setattr(handle_tasks, "_attach_run_tasks", failing_attach)
        with pytest.raises(RuntimeError, match=_START_FAILURE):
            await command.start()

    assert spawned, "the spawn seam must have produced a child for this case"
    assert spawned[0].returncode is not None, (
        "a child spawned before the failure must be reclaimed, not abandoned"
    )
    return events


def test_failed_start_still_settles_the_run() -> None:
    """A start that raises publishes a terminal outcome for the run it began.

    Preparation opens the presentation-sink session and emits ``plan``; that
    framing is already visible to an observer before any child exists. Leaving
    it open behind a raised exception would strand the session and tell a
    consumer a run had begun with nothing to say how it ended, so the failure
    has to close the bracket and report the terminal outcome.

    The spawn is the seam that fails, which is the earliest point at which a
    run has both a plan and an error — the case a caller can never reach by
    asking for a command that does not exist, because that fails later.
    """

    async def run_case() -> None:
        """Fail the spawn and read the run's event stream."""
        command, python_program = _python_command()
        events = await _start_with_failing_spawn(command, python_program)

        assert _phases(events)[0] == "plan", (
            f"the run must be planned before it fails, got {_phases(events)}"
        )
        assert "start" not in _phases(events), (
            "no child was spawned, so no start event may be published"
        )
        assert events[-1].phase == "settled", (
            f"a failed start must settle the run, got {events[-1].phase}"
        )
        assert events[-1].terminal_outcome is TerminalOutcome.ERROR, (
            f"the terminal outcome must be error, got {events[-1].terminal_outcome}"
        )

    asyncio.run(run_case())


def test_failed_task_attachment_is_reclaimed_and_settled() -> None:
    """A failure attaching the child's tasks is owned and reported like the rest.

    Tasks are what drive the child's streams, so a child whose attachment
    failed is a child nobody is draining. The failure must therefore do two
    things: reclaim the child, and settle the run so its ``settled`` event is
    what a consumer sees rather than silence after a ``start``.

    The child is real here — only the seam is a double — so the reclaim is
    observable as a reaped process, not merely as an assumed call.
    """

    async def run_case() -> None:
        """Fail task attachment around a genuinely spawned child."""
        command, python_program = _python_command()
        events = await _start_with_failing_attachment(command, python_program)

        assert "start" in _phases(events), (
            f"the child was spawned, so its start must be announced, got "
            f"{_phases(events)}"
        )
        assert events[-1].phase == "settled", (
            f"the reclaim must still settle the run, got {events[-1].phase}"
        )
        assert events[-1].terminal_outcome is TerminalOutcome.ERROR, (
            f"the terminal outcome must be error, got {events[-1].terminal_outcome}"
        )

    asyncio.run(run_case())


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # One row per redirected stream: a handle forces capture on, so either
        # target contradicts it, and a single row could not show both.
        (
            RunOutputOptions(capture=False, stdout=StdioTarget.inherit()),
            "stdout cannot be redirected",
        ),
        (
            RunOutputOptions(capture=False, stderr=StdioTarget.inherit()),
            "stderr cannot be redirected",
        ),
    ],
)
def test_start_rejects_a_redirected_stream(
    output: RunOutputOptions,
    expected: str,
) -> None:
    """A handle forces capture, so it cannot own a stream bound elsewhere.

    ``start()`` exists so its caller can inspect the child after the fact, and
    a stream bound to the parent has no parent-side pipe to inspect. The
    refusal is raised while the options are resolved — before any child
    exists — so a rejected call leaves nothing running and nothing to reclaim.
    This pins the contract the users' guide states for ``start()``: redirect a
    stream with ``run()`` or ``run_sync()``, not with a handle.
    """

    async def run_case() -> None:
        """Ask a real command to start with the redirected options."""
        command, python_program = _python_command()

        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            pytest.raises(ValueError, match=expected),
        ):
            await command.start(output=output)

    asyncio.run(run_case())


def test_successful_start_leaves_the_session_to_the_handle() -> None:
    """A start that succeeds hands the open session to the handle it returns.

    This is the control for the two cases above: the same observation path
    must not settle a run whose child was spawned and owned. A regression that
    finalized on every path — for instance by closing unconditionally in a
    ``finally`` — would satisfy the failure cases while quietly ending every
    successful run's session before its result arrived.
    """

    async def run_case() -> None:
        """Start a real child and confirm its run is still open."""
        catalogue, python_program = python_catalogue()
        builder = sh.make(python_program, catalogue=catalogue)
        command = builder("-c", "import sys; sys.exit(4)")
        events, observe = _observations()

        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(observe),
        ):
            handle = await command.start()
            result = await handle.wait()

        assert result.exit_code == 4, (
            f"the child's own status must reach the result, got {result.exit_code}"
        )
        assert _phases(events)[-1] == "settled", (
            f"the handle's own settlement must close the run, got {_phases(events)}"
        )

    asyncio.run(run_case())
