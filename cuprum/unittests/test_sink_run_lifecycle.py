"""Run-lifecycle coverage for an active presentation sink.

``cuprum._sink_lifecycle`` promises one session per run and exactly one close
per terminal path. Mapping a failure to a bounded outcome is a pure function
with its own test; these tests are the other half — that the public
``SafeCmd.run``/``run_sync`` and ``Pipeline`` entry points actually reach that
close on the paths where the run does not simply succeed.

The paths here are the ones a happy-path lifecycle test cannot reach: an
asynchronous run, a command cancelled by its caller, and a spawn that fails
before any child exists. Each asserts the close happened exactly once, because
a terminal path that closed twice is a defect the integration owes the adapter.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import ProgramCatalogue, TimeoutExpired, sh
from cuprum.program import Program
from cuprum.sh import RunOutputOptions
from cuprum.sinks import TerminalOutcome
from cuprum.unittests._sink_test_support import RecordingSink
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd

# A cancellation must land while the child is still running, so the command has
# to outlive the caller's own readiness signal by a wide margin.
_CANCEL_AFTER_SECONDS = 0.2
_CHILD_LIFETIME_SECONDS = 5
# Shorter than the child's own lifetime, so at least one keepalive is due
# before the run ends; the driver needs room to poll, hence the margin.
_KEEPALIVE_SECONDS = 0.05


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def _assert_closed_once(adapter: RecordingSink) -> TerminalOutcome:
    """Assert the run's session closed exactly once, returning its outcome."""
    session = adapter.last_session
    assert session.closed == 1, (
        f"every terminal path must close the sink session exactly once; "
        f"got {session.closed} close(s)"
    )
    return adapter.recorded_outcome.outcome


# ---------------------------------------------------------------------------
# Asynchronous runs
# ---------------------------------------------------------------------------


def test_async_run_closes_the_session_once_on_success(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An awaited ``run`` closes its session once, like ``run_sync``.

    The lifecycle tests drive ``run_sync`` only, so nothing else establishes
    that the asynchronous entry point carries the same bracket.
    """
    adapter = RecordingSink()
    command = python_builder("-c", "print('awaited')")

    result = asyncio.run(command.run(output=RunOutputOptions(sink=adapter)))

    assert result.ok is True, f"the awaited run must succeed; got {result!r}"
    assert result.stdout == "awaited\n", (
        f"capture must be unchanged by the sink; got {result.stdout!r}"
    )
    assert adapter.opened == 1, (
        f"one run must consult the adapter once; opened={adapter.opened}"
    )
    outcome = _assert_closed_once(adapter)
    assert outcome == TerminalOutcome.EXIT_ZERO, (
        f"a successful awaited run must close with {TerminalOutcome.EXIT_ZERO}; "
        f"got {outcome}"
    )


def test_async_run_closes_once_when_the_caller_cancels(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A caller-cancelled ``run`` still closes its session exactly once.

    The child outlives the cancellation, so the close has to happen on the
    cancellation path itself rather than after the process settles. The
    outcome is what makes that distinction observable: a close that waited for
    the child would still be pending here, not recorded as cancelled.
    """
    adapter = RecordingSink()
    command = python_builder(
        "-c", f"import time; time.sleep({_CHILD_LIFETIME_SECONDS})"
    )

    async def cancel_the_run() -> None:
        """Start the run, cancel it, and let its cleanup drain."""
        task = asyncio.create_task(command.run(output=RunOutputOptions(sink=adapter)))
        await asyncio.sleep(_CANCEL_AFTER_SECONDS)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_the_run())

    outcome = _assert_closed_once(adapter)
    assert outcome == TerminalOutcome.CANCELLED, (
        f"a cancelled run must close with {TerminalOutcome.CANCELLED}; got {outcome}"
    )


def test_stderr_only_echo_lands_in_the_session_log(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A mirrored stderr reaches the session, and mirrored stdout does not.

    Each stream resolves its own destination, so an implementation that wired
    only stdout's mirror to the session would leave this assertion failing on
    an empty log rather than passing on stdout alone.
    """
    adapter = RecordingSink()
    command = python_builder(
        "-c",
        "import sys; print('out line'); sys.stderr.write('err line\\n')",
    )

    result = command.run_sync(
        output=RunOutputOptions(
            echo=True,
            echo_stdout=False,
            echo_stderr=True,
            sink=adapter,
        ),
    )

    assert result.stdout == "out line\n", (
        f"capture must be unaffected by routing; got {result.stdout!r}"
    )
    assert result.stderr == "err line\n", (
        f"capture must be unaffected by routing; got {result.stderr!r}"
    )
    framed = adapter.last_session.framed
    assert "err line" in framed, (
        f"the mirrored stderr must reach the session log; got {framed!r}"
    )
    assert "out line" not in framed, (
        f"stdout echo is off, so it must not reach the session log; got {framed!r}"
    )


def test_single_command_keepalive_lands_in_the_session_log(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A command's own keepalive is written inside the session's framing.

    The keepalive is the run's second parent-facing writer, beside the mirrored
    streams, and it resolves its destination on its own path. An implementation
    that framed only the streams would leave this line on the parent's stderr
    while the group was open, which is exactly the output the adapter exists to
    own.
    """
    adapter = RecordingSink()
    command = python_builder(
        "-c", f"import time; time.sleep({_CHILD_LIFETIME_SECONDS})"
    )

    result = command.run_sync(
        output=RunOutputOptions(sink=adapter, idle_after=_KEEPALIVE_SECONDS),
    )

    assert result.ok is True, f"the idle run must succeed; got {result!r}"
    framed = adapter.last_session.framed
    assert "[cuprum] still running" in framed, (
        f"the keepalive must be written through the session, not beside it; "
        f"the session saw {framed!r}"
    )


def test_async_run_closes_when_the_program_cannot_be_spawned() -> None:
    """A spawn failure closes the session with the bounded error outcome.

    Nothing here waits on a child: the program names a path that does not
    exist, so ``create_subprocess_exec`` fails before any process is created.
    The adapter must still be finalized, and no exception text may reach it.
    """
    adapter = RecordingSink()
    _, python_program = python_catalogue()
    absent_program = Program(f"{python_program}.does-not-exist")
    absent = sh.make(
        absent_program,
        catalogue=ProgramCatalogue.from_programs(
            absent_program,
            name="absent-program",
        ),
    )("-c", "pass")

    with pytest.raises(FileNotFoundError):
        asyncio.run(absent.run(output=RunOutputOptions(sink=adapter)))

    outcome = _assert_closed_once(adapter)
    assert outcome == TerminalOutcome.ERROR, (
        f"a spawn failure must close with {TerminalOutcome.ERROR}; got {outcome}"
    )
    recorded = adapter.recorded_outcome
    assert recorded.exit_code is None, (
        f"a run that never spawned has no exit code; got {recorded.exit_code}"
    )
    assert recorded.detail is None, (
        f"the adapter must not receive exception text; got {recorded.detail!r}"
    )


# ---------------------------------------------------------------------------
# Pipeline deadline
# ---------------------------------------------------------------------------


def test_pipeline_timeout_closes_the_session_once(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An expired pipeline deadline closes its session once, as a timeout.

    The pipeline enforces one deadline for the whole run, so this is the
    pipeline's own terminal path rather than a stage's: it must close the
    session it opened, and report the timeout category rather than a
    synthesized exit code.
    """
    adapter = RecordingSink()
    producer = python_builder(
        "-c", f"import time; time.sleep({_CHILD_LIFETIME_SECONDS})"
    )
    consumer = python_builder("-c", "import sys; sys.stdin.read()")

    with pytest.raises(TimeoutExpired, match=r"timed out"):
        (producer | consumer).run_sync(
            output=RunOutputOptions(sink=adapter),
            timeout=_CANCEL_AFTER_SECONDS,
        )

    outcome = _assert_closed_once(adapter)
    assert outcome == TerminalOutcome.TIMEOUT, (
        f"an expired pipeline must close with {TerminalOutcome.TIMEOUT}; got {outcome}"
    )
    recorded = adapter.recorded_outcome
    assert recorded.exit_code is None, (
        f"a timeout must carry no exit code; got {recorded.exit_code}"
    )
    assert recorded.detail == "timeout", (
        f"a timeout must report its bounded categorical detail; got {recorded.detail!r}"
    )
