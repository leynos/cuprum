"""Behavioural tests for the ``RunningCommand`` handle.

Each scenario's ``When`` step runs that scenario's whole story inside one event
loop and returns a
:class:`~tests.behaviour._running_command_support.HandleObservation`; the
``Then`` steps assert against that record. The shape is forced by the artefact
under test: a handle holds an :class:`asyncio.Lock` and an asyncio subprocess
bound to the loop that spawned them, so it cannot be carried across two
``asyncio.run`` calls, and a scenario split over several steps would have to
re-open a loop mid-story.

The assertions are about what a caller can observe: the outcome the handle
recorded, the signal return code it reports, and whether the child was in fact
reaped. Nothing here reaches inside the handle; the stub-process tests in
``cuprum/unittests/test_running_command_lifecycle.py`` do that.
"""

from __future__ import annotations

import signal
import sys
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum.sh import SignalAfterExitError, TimeoutExpired, UnsupportedSignalError
from tests.behaviour._running_command_control_support import (
    _observe_cancelled_teardown,
    _observe_context_exit,
    _observe_context_exit_with_error,
    _observe_escalating_termination,
    _observe_graceful_termination,
    _observe_kill,
    _observe_repeated_termination,
    _observe_unsupported_signal,
)
from tests.behaviour._running_command_support import (
    DELAYED_WRITER_MARKER,
    HandleObservation,
    ReadyChild,
    _absent_start_command,
    _delayed_writer_command,
    _exit_code_command,
    _observe_concurrent_waits,
    _observe_early_exit_signal,
    _observe_failed_start,
    _observe_output_wait,
    _observe_start,
    _observe_wait_deadline,
    _ready_child,
    _sleep_command,
)
from tests.helpers.timeouts import CHILD_STDOUT

if typ.TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The handle's control operations rely on POSIX signal semantics.",
)

#: Status the output-producing child exits with, non-zero so a signal-reported
#: code cannot pass for it.
_OUTPUT_EXIT_CODE = 7

#: Status the early-exit child chooses for itself, so a refusal that reports
#: the wrong code cannot pass by coincidence.
_EARLY_EXIT_CODE = 3


@scenario(
    "../features/running_command.feature",
    "Start returns an owned handle before the child exits",
)
def test_start_returns_owned_handle() -> None:
    """Behavioural coverage for the handle's ownership contract."""


@scenario(
    "../features/running_command.feature",
    "Wait returns the child's outcome and captures its output",
)
def test_wait_returns_outcome() -> None:
    """Behavioural coverage for awaiting a started child's outcome."""


@scenario(
    "../features/running_command.feature",
    "Startup failure is owned by start",
)
def test_startup_failure_is_owned() -> None:
    """Behavioural coverage for a spawn that cannot start."""


@scenario(
    "../features/running_command.feature",
    "A child that exits before any signal is refused",
)
def test_signal_after_exit_is_refused() -> None:
    """Behavioural coverage for signalling a child that has already exited."""


@scenario(
    "../features/running_command.feature",
    "Readiness timeout settles the handle",
)
def test_readiness_timeout_settles() -> None:
    """Behavioural coverage for exhausting a handle's wait deadline."""


@scenario(
    "../features/running_command.feature",
    "Concurrent waits settle the command once",
)
def test_concurrent_waits_settle_once() -> None:
    """Behavioural coverage for several waiters sharing one settlement."""


@scenario(
    "../features/running_command.feature",
    "Terminate stops a cooperative child gracefully",
)
def test_terminate_cooperative_child() -> None:
    """Behavioural coverage for the graceful stop."""


@scenario(
    "../features/running_command.feature",
    "Terminate escalates a non-cooperative child",
)
def test_terminate_escalates_non_cooperative_child() -> None:
    """Behavioural coverage for the forced escalation."""


@scenario(
    "../features/running_command.feature",
    "Kill stops a child without asking",
)
def test_kill_stops_child() -> None:
    """Behavioural coverage for the immediate stop."""


@scenario(
    "../features/running_command.feature",
    "Repeated termination is idempotent",
)
def test_repeated_termination_is_idempotent() -> None:
    """Behavioural coverage for repeating a stop on one handle."""


@scenario(
    "../features/running_command.feature",
    "Context-manager exit settles a running child",
)
def test_context_exit_settles() -> None:
    """Behavioural coverage for the context manager's teardown guarantee."""


@scenario(
    "../features/running_command.feature",
    "Context-manager exit does not suppress the block's error",
)
def test_context_exit_does_not_suppress() -> None:
    """Behavioural coverage for exception transparency on exit."""


@scenario(
    "../features/running_command.feature",
    "Cancellation during context-manager teardown still settles",
)
def test_cancelled_teardown_still_settles() -> None:
    """Behavioural coverage for cancellation arriving during teardown."""


@scenario(
    "../features/running_command.feature",
    "A platform that cannot deliver the signal refuses it",
)
def test_unsupported_platform_signal_refused() -> None:
    """Behavioural coverage for the unsupported-platform refusal."""


@given(
    "a safe command whose child blocks until stopped",
    target_fixture="blocking_command",
)
def given_blocking_command(tmp_path: Path) -> object:
    """Build a command whose child blocks until something stops it.

    Returns
    -------
    SafeCmd
        A command whose child runs for far longer than any scenario waits.
    """
    return _sleep_command(tmp_path)


@given(
    "a safe command whose child exits immediately with status 7",
    target_fixture="output_command",
)
def given_output_command(tmp_path: Path) -> object:
    """Build a command that writes to stdout, then exits with status 7.

    Returns
    -------
    SafeCmd
        A command producing capturable output and a non-zero status.
    """
    return _exit_code_command(tmp_path, _OUTPUT_EXIT_CODE, writes=True)


@given(
    "a safe command whose child exits immediately with status 3",
    target_fixture="early_exit_command",
)
def given_early_exit_command(tmp_path: Path) -> object:
    """Build a command that ends at once with status 3.

    Returns
    -------
    SafeCmd
        A command whose child exits before any caller can signal it.
    """
    return _exit_code_command(tmp_path, _EARLY_EXIT_CODE)


@given(
    "a safe command whose child writes after a delay",
    target_fixture="delayed_writer_command",
)
def given_delayed_writer_command(tmp_path: Path) -> object:
    """Build a command that writes its marker only after a short delay.

    Returns
    -------
    SafeCmd
        A command whose output arrives after its waits have been issued.
    """
    return _delayed_writer_command(tmp_path)


@given("a registered but absent start executable", target_fixture="absent_command")
def given_absent_executable(tmp_path: Path) -> object:
    """Build an allowlisted command whose absolute path does not exist.

    Returns
    -------
    SafeCmd
        A command naming a program that is not on disk.
    """
    return _absent_start_command(tmp_path)


@given("a child that blocks until stopped", target_fixture="cooperative_child")
def given_cooperative_child(tmp_path: Path) -> ReadyChild:
    """Build a child that obeys ``SIGTERM`` and blocks until stopped.

    Returns
    -------
    ReadyChild
        The command, and the marker its child writes once it is running.
    """
    return _ready_child(tmp_path, stubborn=False)


@given("a child that ignores SIGTERM and blocks", target_fixture="stubborn_child")
def given_stubborn_child(tmp_path: Path) -> ReadyChild:
    """Build a child that ignores ``SIGTERM`` and blocks until forced.

    Returns
    -------
    ReadyChild
        The command, and the marker its child writes once it is running.
    """
    return _ready_child(tmp_path, stubborn=True)


@when("I start it and hold the handle", target_fixture="observation")
def when_start_and_hold(cooperative_child: ReadyChild) -> HandleObservation:
    """Start the blocking child and report what its handle exposed.

    Returns
    -------
    HandleObservation
        The handle's PID, execution identifier, and the child's liveness.
    """
    return _observe_start(cooperative_child)


@when("I start it and await its outcome", target_fixture="observation")
def when_start_and_await(output_command: object) -> HandleObservation:
    """Start the output-producing child and await the outcome it settles.

    Returns
    -------
    HandleObservation
        The settled outcome, carrying the child's captured output.
    """
    return _observe_output_wait(output_command)


@when("I start it", target_fixture="observation")
def when_start_absent(absent_command: object) -> HandleObservation:
    """Start the absent executable and carry out whatever failure it raises.

    Returns
    -------
    HandleObservation
        The failure that reached the caller, and the absence of a child.
    """
    return _observe_failed_start(absent_command)


@when("I start it, await its outcome, and signal it", target_fixture="observation")
def when_start_await_and_signal(early_exit_command: object) -> HandleObservation:
    """Await a child that exited early, then attempt to signal it.

    Returns
    -------
    HandleObservation
        The settled outcome, and the refusal the signal raised.
    """
    return _observe_early_exit_signal(early_exit_command, signal.SIGTERM)


@when("I start it and wait with an elapsed deadline", target_fixture="observation")
def when_wait_with_elapsed_deadline(blocking_command: object) -> HandleObservation:
    """Start the blocking child and wait under an already-elapsed deadline.

    Returns
    -------
    HandleObservation
        The timeout that reached the caller, and whether the child was reaped.
    """
    return _observe_wait_deadline(blocking_command)


@when("I issue several waits on one handle at once", target_fixture="observation")
def when_issue_concurrent_waits(delayed_writer_command: object) -> HandleObservation:
    """Issue several waits on one handle simultaneously.

    Returns
    -------
    HandleObservation
        Every waiter's outcome, in the order the waits were issued.
    """
    return _observe_concurrent_waits(delayed_writer_command)


@when("I start it and terminate it", target_fixture="observation")
def when_start_and_terminate(cooperative_child: ReadyChild) -> HandleObservation:
    """Terminate the cooperative child and report the outcome.

    Returns
    -------
    HandleObservation
        The outcome, reporting the signal that ended the child.
    """
    return _observe_graceful_termination(cooperative_child)


@when(
    "I start it and terminate it with a short grace period",
    target_fixture="observation",
)
def when_start_and_terminate_with_short_grace(
    stubborn_child: ReadyChild,
) -> HandleObservation:
    """Terminate the stubborn child under a short escalation grace.

    Returns
    -------
    HandleObservation
        The outcome, reporting the forced signal that ended the child.
    """
    return _observe_escalating_termination(stubborn_child)


@when("I start it and kill it", target_fixture="observation")
def when_start_and_kill(cooperative_child: ReadyChild) -> HandleObservation:
    """Compel the cooperative child to exit without asking it first.

    Returns
    -------
    HandleObservation
        The outcome, reporting the forced signal.
    """
    return _observe_kill(cooperative_child)


@when("I start it and terminate it twice", target_fixture="observation")
def when_start_and_terminate_twice(cooperative_child: ReadyChild) -> HandleObservation:
    """Terminate the same handle twice.

    Returns
    -------
    HandleObservation
        Both outcomes, so the scenario can check they are one object.
    """
    return _observe_repeated_termination(cooperative_child)


@when("I leave the handle's context while the child runs", target_fixture="observation")
def when_leave_context(cooperative_child: ReadyChild) -> HandleObservation:
    """Enter the handle's context, then leave while the child still runs.

    Returns
    -------
    HandleObservation
        The outcome the context exit settled the handle with.
    """
    return _observe_context_exit(cooperative_child)


@when("I leave the handle's context with an error", target_fixture="observation")
def when_leave_context_with_error(cooperative_child: ReadyChild) -> HandleObservation:
    """Leave the handle's context with the block already failing.

    Returns
    -------
    HandleObservation
        The error that reached the caller, and the settled outcome.
    """
    return _observe_context_exit_with_error(cooperative_child)


@when(
    "I leave the handle's context while teardown is cancelled",
    target_fixture="observation",
)
def when_leave_context_cancelled(stubborn_child: ReadyChild) -> HandleObservation:
    """Cancel the task leaving the handle's context, mid-teardown.

    Returns
    -------
    HandleObservation
        The settled outcome, proving teardown finished despite cancellation.
    """
    return _observe_cancelled_teardown(stubborn_child)


@when("I signal a live child on an unsupported platform", target_fixture="observation")
def when_signal_on_unsupported_platform(
    cooperative_child: ReadyChild,
) -> HandleObservation:
    """Signal a running child on a platform that cannot deliver the signal.

    Returns
    -------
    HandleObservation
        The refusal that reached the caller.
    """
    return _observe_unsupported_signal(cooperative_child, signal.SIGTERM)


@then("the handle reports the running child's identifier")
def then_handle_reports_identifier(observation: HandleObservation) -> None:
    """Assert the handle exposes a live child's PID and execution identifier."""
    assert observation.handle_pid is not None, "a spawned child reports its PID"
    assert observation.handle_pid > 0, "a reported PID must be a real identifier"
    assert observation.exec_id, "the handle carries its run's execution identifier"
    assert observation.result is None, "a running child has no settled result yet"


@then("the child is still running when the handle is returned")
def then_child_still_running(observation: HandleObservation) -> None:
    """Assert the child outlived the call that returned its handle."""
    assert observation.child_was_live, (
        "start() must return a handle owning a child that is still running"
    )


@then("the handle reports the command's exit code")
def then_handle_reports_exit_code(observation: HandleObservation) -> None:
    """Assert the settled result carries the child's own exit status."""
    result = observation.result
    assert result is not None, "a waited-on child must settle a result"
    assert result.exit_code == _OUTPUT_EXIT_CODE, (
        f"expected the child's own exit code {_OUTPUT_EXIT_CODE}, "
        f"got {result.exit_code}"
    )


@then("the handle reports the command's captured output")
def then_handle_reports_captured_output(observation: HandleObservation) -> None:
    """Assert the settled result carries what the child wrote."""
    result = observation.result
    assert result is not None, "a waited-on child must settle a result"
    assert result.stdout is not None, "capture is on by default for a handle"
    assert CHILD_STDOUT in result.stdout, (
        f"the handle must capture the child's stdout, got {result.stdout!r}"
    )


@then("start reports the spawn failure")
def then_start_reports_failure(observation: HandleObservation) -> None:
    """Assert the absent executable's spawn failure reached the caller."""
    assert isinstance(observation.error, FileNotFoundError), (
        "a spawn of an absent executable must raise FileNotFoundError, got "
        f"{observation.error!r}"
    )


@then("the failed start leaves no orphaned child")
def then_failed_start_leaves_no_child(observation: HandleObservation) -> None:
    """Assert a start that could not spawn reported no child to own."""
    assert observation.handle_pid is None, (
        "a start that never spawned must not report a child's PID"
    )


@then("the signal is refused because the child has already exited")
def then_signal_refused(observation: HandleObservation) -> None:
    """Assert the refusal carries the exit code the handle recorded."""
    result = observation.result
    assert result is not None, "the child must have been awaited first"
    error = observation.error
    assert isinstance(error, SignalAfterExitError), (
        f"signalling an exited child must raise SignalAfterExitError, got {error!r}"
    )
    assert error.exit_code == result.exit_code, (
        "the refusal must carry the exit code the handle itself recorded"
    )
    assert error.exit_code == _EARLY_EXIT_CODE, (
        f"the refusal must carry the child's own status, got {error.exit_code}"
    )


@then("the exited child's process has gone")
def then_exited_child_gone(observation: HandleObservation) -> None:
    """Assert the child that exited early was reaped, not left as a zombie."""
    assert observation.child_reaped, (
        "a child that exited must be reaped by the handle that owns it"
    )


@then("the handle reports a timeout error")
def then_handle_reports_timeout(observation: HandleObservation) -> None:
    """Assert the elapsed deadline surfaced as the public timeout."""
    assert isinstance(observation.error, TimeoutExpired), (
        "an elapsed deadline must raise TimeoutExpired"
    )


@then("the stopped child has been reaped")
def then_deadline_child_reaped(observation: HandleObservation) -> None:
    """Assert the deadline stop left no child running."""
    assert observation.child_reaped, (
        "a wait that exhausted its deadline must still reap the child"
    )


@then("every concurrent wait reports the same outcome")
def then_concurrent_waits_agree(observation: HandleObservation) -> None:
    """Assert several waiters shared one settlement, and which one it was."""
    assert len(observation.results) == 5, "the scenario records every waiter"
    first = observation.results[0]
    assert all(outcome is first for outcome in observation.results), (
        "every concurrent wait must observe the one settlement, not build its own"
    )
    assert first.stdout == DELAYED_WRITER_MARKER + "\n", (
        f"the settlement must carry the child's output, got {first.stdout!r}"
    )


@then("the concurrently awaited child has been reaped")
def then_concurrent_child_reaped(observation: HandleObservation) -> None:
    """Assert the concurrently awaited child was reaped."""
    assert observation.child_reaped, (
        "the child behind a concurrent settlement must be reaped"
    )


@then("the worker's result reports the graceful signal")
def then_worker_reports_graceful(observation: HandleObservation) -> None:
    """Assert a child that obeyed ``SIGTERM`` reports that signal."""
    result = observation.result
    assert result is not None, "a terminated handle must settle a result"
    assert result.exit_code == -signal.SIGTERM, (
        f"a cooperative terminate must report -{signal.SIGTERM}, got {result.exit_code}"
    )


@then("the worker's result reports the forced signal")
def then_worker_reports_forced_signal(observation: HandleObservation) -> None:
    """Assert a child that had to be forced reports ``SIGKILL``."""
    result = observation.result
    assert result is not None, "a terminated handle must settle a result"
    assert result.exit_code == -signal.SIGKILL, (
        f"a forced stop must report -{signal.SIGKILL}, got {result.exit_code}"
    )


@then("the worker's captured output is preserved")
def then_worker_output_preserved(observation: HandleObservation) -> None:
    """Assert the child's output survived its own termination."""
    result = observation.result
    assert result is not None, "a terminated handle must settle a result"
    assert result.stdout is not None, "a handle captures its child's output"
    assert CHILD_STDOUT in result.stdout, (
        f"termination must not discard captured output, got {result.stdout!r}"
    )


@then("the terminated worker has been reaped")
def then_terminated_worker_reaped(observation: HandleObservation) -> None:
    """Assert a gracefully terminated child is gone once control returns."""
    assert observation.child_reaped, "a terminated child must be reaped"


@then("the escalated worker has been reaped")
def then_escalated_worker_reaped(observation: HandleObservation) -> None:
    """Assert an escalation to ``SIGKILL`` left no child behind."""
    assert observation.child_reaped, "an escalation to SIGKILL must reap the child"


@then("the killed worker has been reaped")
def then_killed_worker_reaped(observation: HandleObservation) -> None:
    """Assert a killed child is reaped before its outcome is reported."""
    assert observation.child_reaped, "a killed child must be reaped"


@then("the repeatedly terminated worker has been reaped")
def then_repeatedly_terminated_reaped(observation: HandleObservation) -> None:
    """Assert the second stop left no child behind."""
    assert observation.child_reaped, "a repeatedly terminated child must be reaped"


@then("the context-managed worker has been reaped")
def then_context_managed_worker_reaped(observation: HandleObservation) -> None:
    """Assert context exit reaped the child it stopped."""
    assert observation.child_reaped, "context exit must reap the child it stopped"


@then("the suppressed-check worker has been reaped")
def then_suppression_check_worker_reaped(observation: HandleObservation) -> None:
    """Assert teardown reaped the child even while the block was failing."""
    assert observation.child_reaped, (
        "teardown must reap the child even when the block raises"
    )


@then("the cancellation-settled worker has been reaped")
def then_cancelled_worker_reaped(observation: HandleObservation) -> None:
    """Assert a cancelled teardown still reaped the child."""
    assert observation.child_reaped, "a cancelled teardown must still reap the child"


@then("both terminations report one outcome")
def then_both_terminations_agree(observation: HandleObservation) -> None:
    """Assert repeating a stop returns the one recorded settlement."""
    outcomes = observation.results
    assert len(outcomes) == 2, "the scenario must record both stop outcomes"
    first, second = outcomes
    assert first is second, (
        "repeating a stop must return the recorded outcome, not build a new one"
    )


@then("the context exit settled the handle")
def then_context_exit_settled(observation: HandleObservation) -> None:
    """Assert context exit recorded an outcome and stopped the child."""
    result = observation.result
    assert result is not None, "context exit must record a settled result"
    assert result.exit_code == -signal.SIGTERM, (
        f"context exit must stop a running child gracefully, got {result.exit_code}"
    )


@then("the block's error reaches the caller")
def then_block_error_reaches_caller(observation: HandleObservation) -> None:
    """Assert teardown did not replace the block's own exception."""
    assert isinstance(observation.error, ValueError), (
        f"the block's error must reach the caller unchanged, got {observation.error!r}"
    )
    assert str(observation.error) == "block error", (
        "the block's message must survive teardown intact"
    )
    assert observation.result is not None, (
        "teardown must still settle the handle while the block fails"
    )


@then("the cancelled teardown still settled the handle")
def then_cancelled_teardown_settled(observation: HandleObservation) -> None:
    """Assert a cancellation mid-teardown still recorded the outcome."""
    assert observation.result is not None, (
        "a cancellation during teardown must not leave the handle unsettled"
    )


@then("the unsupported platform refuses the signal")
def then_unsupported_platform_refuses(observation: HandleObservation) -> None:
    """Assert the platform limitation is named rather than the child's state."""
    assert isinstance(observation.error, UnsupportedSignalError), (
        "a platform that cannot deliver a caller-named signal must raise "
        f"UnsupportedSignalError, got {observation.error!r}"
    )


@then("the refused signal names what was asked for")
def then_refused_signal_names_request(observation: HandleObservation) -> None:
    """Assert the refusal carries the requested signal and the platform name."""
    error = observation.error
    assert isinstance(error, UnsupportedSignalError), "the refusal must be typed"
    assert error.signal_number == signal.SIGTERM, (
        f"the refusal must name the signal asked for, got {error.signal_number}"
    )
    assert error.platform_name, "the refusal must name the platform that declined"
