"""Behavioural tests for the handle's stop, signal, and teardown scenarios.

The scenarios are declared here, and so are the ``Then`` steps: those carry the
assertions, which only ``test_*.py`` files may use. The ``Given`` and ``When``
steps live in ``_running_command_steps.py``, loaded below as a plugin, and the
orchestration they call lives in ``_running_command_control_support.py``. The
start and wait scenarios are the companion ``test_running_command.py``.

The split between the two test modules follows the split between their
orchestrations: a scenario that stops a child has to arrange a child that
blocks, wait for it to be genuinely running, act, and then confirm it was
reaped — a different story from starting a child and reading its outcome.

Everything asserted here is what a caller observes after a stop: the signal the
outcome reports, whether captured output survived, and whether the child was in
fact reaped. The handle's internal bookkeeping under those same operations is
covered by the stub-process tests in
``cuprum/unittests/test_running_command_lifecycle.py``.
"""

from __future__ import annotations

import signal
import sys
import typing as typ

import pytest
from pytest_bdd import scenario, then

from cuprum.sh import UnsupportedSignalError
from tests.helpers.timeouts import CHILD_STDOUT

if typ.TYPE_CHECKING:
    from tests.behaviour._running_command_children import HandleObservation

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="The handle's control operations rely on POSIX signal semantics.",
)

# pytest-bdd resolves a step by looking for a fixture that it created in the
# *calling module's* namespace, so a step defined in another module is invisible
# unless that module defines the scenario too. Loading the step module as a
# plugin makes pytest collect its fixtures, which is what registers its
# Given/When steps here without restating the step text.
pytest_plugins = ("tests.behaviour._running_command_steps",)


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
