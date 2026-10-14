"""Behavioural tests for starting a handle, waiting on it, and reading it.

The scenarios are declared here, and so are the ``Then`` steps: those carry the
assertions, which only ``test_*.py`` files may use. The ``Given`` and ``When``
steps live in ``_running_command_steps.py``, loaded below as a plugin, and the
orchestration they call lives in ``_running_command_support.py``. The handle's
stop, signal, and teardown scenarios are the companion
``test_running_command_control.py``.

Each scenario's ``When`` step runs that scenario's whole story inside one event
loop and returns a
:class:`~tests.behaviour._running_command_children.HandleObservation`; the
``Then`` steps assert against that record. The shape is forced by the artefact
under test: a handle holds an :class:`asyncio.Lock` and an asyncio subprocess
bound to the loop that spawned them, so it cannot be carried across two
``asyncio.run`` calls, and a scenario split over several steps would have to
re-open a loop mid-story.

The assertions are about what a caller can observe: the outcome the handle
recorded, the status it reports, and whether the child was in fact reaped.
Nothing here reaches inside the handle; the stub-process tests under
``cuprum/unittests/test_running_command_*.py`` do that.
"""

from __future__ import annotations

import sys

import pytest
from pytest_bdd import scenario, then

from cuprum.sh import SignalAfterExitError, TimeoutExpired
from tests.behaviour._running_command_children import (
    _EARLY_EXIT_CODE,
    _OUTPUT_EXIT_CODE,
    DELAYED_WRITER_MARKER,
    HandleObservation,
)
from tests.helpers.process_state import can_list_processes
from tests.helpers.timeouts import CHILD_STDOUT

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

#: How many waiters the concurrent-wait scenario issues.
_WAITERS = 5


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
@pytest.mark.skipif(
    not can_list_processes(),
    reason=(
        "The orphan check compares this process's child listings, which a host "
        "without a readable procfs cannot provide."
    ),
)
def test_startup_failure_is_owned() -> None:
    """Behavioural coverage for a spawn that cannot start.

    The scenario's second assertion is the orphan check, and it reads the
    children this test process has before and after the attempt. That reading
    is only available where procfs can be enumerated, so the scenario is gated
    on the capability rather than left to pass vacuously on a host that
    reports two empty listings for an unrelated reason. The module mark skips
    Windows for the handle's POSIX signal semantics; this mark narrows the one
    scenario whose oracle is a process listing, leaving the rest of the module
    to run wherever the signals do.
    """


@scenario(
    "../features/running_command.feature",
    "A child that exits before any signal is refused",
)
def test_signal_after_exit_is_refused() -> None:
    """Behavioural coverage for signalling a child that has already exited."""


@scenario(
    "../features/running_command.feature",
    "A child that exits while the handle is merely held is refused",
)
def test_signal_unwaited_exit_is_refused() -> None:
    """Behavioural coverage for signalling before anything settles the handle."""


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
    """Assert the failed start left no process behind to own.

    The handle reports no PID for a start that failed, so its own fields cannot
    witness a leak: a PID field that is structurally ``None`` passes whatever
    the library does. What the step reads instead is the test process's own
    children, compared across the attempt — a spawn that leaked would appear
    there, and a start that never reached its spawn would not.

    The comparison is only an oracle where both listings were actually read,
    so that is asserted before the empty difference is read as proof. A reading
    that could not be taken is not an empty set of children, and the difference
    of two of them is empty whatever the library did; treating it as a passing
    check is the one way this step could report success for a leaking child.
    """
    assert observation.child_listing_available, (
        "the orphan check compares two listings of this process's children, "
        "so both readings must have actually been taken; a reading that could "
        "not be taken produces an empty difference that proves nothing"
    )
    assert not observation.spawned_children, (
        "a start that failed before its child existed must leave no process "
        f"behind, got child PIDs {sorted(observation.spawned_children)}"
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


@then("the refusal reports the child's own status before any wait")
def then_refusal_has_unsettled_status(observation: HandleObservation) -> None:
    """Assert the refusal carried a status no wait had yet published.

    The exit code is the fact under test, and reading it from ``result`` would
    let the assertion pass for a handle that only refused *because* a wait had
    already published it. The child's own status is the oracle, and the
    handle's own field says whether it had published anything at all when the
    refusal was raised.
    """
    assert observation.unsettled_when_refused, (
        "the handle must have settled nothing when it refused the signal"
    )
    error = observation.error
    assert isinstance(error, SignalAfterExitError), (
        f"signalling an exited child must raise SignalAfterExitError, got {error!r}"
    )
    assert error.exit_code == _EARLY_EXIT_CODE, (
        "a refusal raised before any wait must still report the child's own "
        f"status {_EARLY_EXIT_CODE}, got {error.exit_code}"
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
    assert len(observation.results) == _WAITERS, "the scenario records every waiter"
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
