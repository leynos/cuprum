"""Given/When step definitions for the handle scenarios.

The companion modules
:mod:`tests.behaviour._running_command_support` and
:mod:`tests.behaviour._running_command_control_support` drive the scenarios;
:mod:`tests.behaviour._running_command_children` builds the children they
drive. This module holds the ``Given`` and ``When`` steps.
:mod:`tests.behaviour.test_running_command` declares the scenarios and owns the
``Then`` steps, so every step defined here is reachable from a collected test;
the assertions live there because only ``test_*.py`` files may use ``assert``.

Each step body is a single call, so a step is a name for an orchestration
rather than a second place for one.
"""

from __future__ import annotations

import signal
import typing as typ

from pytest_bdd import given, when

from tests.behaviour._running_command_children import (
    _EARLY_EXIT_CODE,
    _OUTPUT_EXIT_CODE,
    ReadyChild,
    _absent_start_command,
    _delayed_writer_command,
    _exit_code_command,
    _ready_child,
    _sleep_command,
)
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
    HandleObservation,
    _observe_concurrent_waits,
    _observe_early_exit_signal,
    _observe_failed_start,
    _observe_output_wait,
    _observe_start,
    _observe_unwaited_exit_signal,
    _observe_wait_deadline,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from cuprum.sh import SafeCmd


@given(
    "a safe command whose child blocks until stopped",
    target_fixture="blocking_command",
)
def given_blocking_command(tmp_path: Path) -> SafeCmd:
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
def given_output_command(tmp_path: Path) -> SafeCmd:
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
def given_early_exit_command(tmp_path: Path) -> SafeCmd:
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
def given_delayed_writer_command(tmp_path: Path) -> SafeCmd:
    """Build a command that writes its marker only after a short delay.

    Returns
    -------
    SafeCmd
        A command whose output arrives after its waits have been issued.
    """
    return _delayed_writer_command(tmp_path)


@given("a registered but absent start executable", target_fixture="absent_command")
def given_absent_executable(tmp_path: Path) -> SafeCmd:
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
def when_start_and_await(output_command: SafeCmd) -> HandleObservation:
    """Start the output-producing child and await the outcome it settles.

    Returns
    -------
    HandleObservation
        The settled outcome, carrying the child's captured output.
    """
    return _observe_output_wait(output_command)


@when("I start it", target_fixture="observation")
def when_start_absent(absent_command: SafeCmd) -> HandleObservation:
    """Start the absent executable and carry out whatever failure it raises.

    Returns
    -------
    HandleObservation
        The failure that reached the caller, and the absence of a child.
    """
    return _observe_failed_start(absent_command)


@when("I start it, await its outcome, and signal it", target_fixture="observation")
def when_start_await_and_signal(early_exit_command: SafeCmd) -> HandleObservation:
    """Await a child that exited early, then attempt to signal it.

    Returns
    -------
    HandleObservation
        The settled outcome, and the refusal the signal raised.
    """
    return _observe_early_exit_signal(early_exit_command, signal.SIGTERM)


@when(
    "I start it and signal it without awaiting its outcome",
    target_fixture="observation",
)
def when_start_and_signal_unwaited(early_exit_command: SafeCmd) -> HandleObservation:
    """Signal a child that exited while its handle was only held.

    Returns
    -------
    HandleObservation
        The refusal, the handle's unsettled state when it was raised, and
        whether the child was reaped.
    """
    return _observe_unwaited_exit_signal(early_exit_command, signal.SIGTERM)


@when("I start it and wait with an elapsed deadline", target_fixture="observation")
def when_wait_with_elapsed_deadline(blocking_command: SafeCmd) -> HandleObservation:
    """Start the blocking child and wait under an already-elapsed deadline.

    Returns
    -------
    HandleObservation
        The timeout that reached the caller, and whether the child was reaped.
    """
    return _observe_wait_deadline(blocking_command)


@when("I issue several waits on one handle at once", target_fixture="observation")
def when_issue_concurrent_waits(delayed_writer_command: SafeCmd) -> HandleObservation:
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
    monkeypatch: pytest.MonkeyPatch,
) -> HandleObservation:
    """Signal a running child on a platform that cannot deliver the signal.

    Returns
    -------
    HandleObservation
        The refusal that reached the caller.
    """
    return _observe_unsupported_signal(
        cooperative_child,
        signal.SIGTERM,
        monkeypatch,
    )
