"""Behavioural coverage for the opt-in broken-pipe echo policy (#435).

These scenarios exercise the contract the ticket describes end to end: a real
child process, a real drain, and a presentation sink whose destination has
genuinely closed. The drain-level invariants live in
``cuprum/unittests/test_broken_pipe_echo_guard.py``; what is asserted here is
that the policy a caller names on ``RunOutputOptions`` reaches a real run and
changes what ``run_sync`` returns.
"""

from __future__ import annotations

import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import BrokenPipePolicy, RelayFallback, sh
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder, python_catalogue

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd, SafeCmdBuilder

_CLOSED_READER = "closed presentation destination"
_SCRIPT = "print('hello')"
_EXPECTED_STDOUT = "hello\n"
_EXPECTED_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.BROKEN_PIPE,
)


class _BrokenPipeSink:
    """Presentation sink whose destination has closed under the run.

    Records what it was offered so a scenario can prove echo stopped rather
    than retried, and raises on every call — a genuinely closed reader does
    not recover partway through a run.
    """

    def __init__(self) -> None:
        """Record each attempted write before failing."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the attempt, then fail the way a closed reader does."""
        self.attempts.append(payload)
        raise BrokenPipeError(_CLOSED_READER)

    def flush(self) -> None:
        """Model the flush call on a broken stream."""


class _BrokenPipeFixture(typ.TypedDict):
    """State one scenario threads between its steps.

    Attributes
    ----------
    sink : _BrokenPipeSink
        The closed destination every echoed write is aimed at.
    lines : list[str]
        Lines the run observed through ``on_line``, which must keep arriving.
    result : CommandResult
        The result the best-effort run returned.
    error : BaseException
        The error the strict run raised.
    """

    sink: _BrokenPipeSink
    lines: list[str]
    result: CommandResult
    error: BaseException


@pytest.fixture
def command_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return python_builder()


@pytest.fixture
def scenario_state() -> _BrokenPipeFixture:
    """Provide fresh per-scenario state."""
    return typ.cast("_BrokenPipeFixture", {})


def _options(policy: BrokenPipePolicy) -> RunOutputOptions:
    """Build echoed, captured options under *policy*."""
    return RunOutputOptions(
        capture=True,
        echo_stdout=True,
        broken_pipe_policy=policy,
    )


@given("a curated Python command for testing", target_fixture="python_cmd_fixture")
def _given_python_command() -> dict[str, object]:
    """Set up an allowlisted Python command, as the shared background states.

    The step is declared per module wherever it is used rather than registered
    once: pytest-bdd resolves step definitions per module, so a module-local
    declaration is what keeps these scenarios collectable in isolation.

    Returns
    -------
    dict[str, object]
        The catalogue, Python program and command builder for the scenario.
    """
    catalogue, python_program = python_catalogue()
    return {
        "catalogue": catalogue,
        "python_program": python_program,
        "builder": sh.make(python_program, catalogue=catalogue),
    }


@given(
    "a presentation sink whose destination has closed",
    target_fixture="scenario_state",
)
def _closed_sink() -> _BrokenPipeFixture:
    """Install a sink that fails every write as a closed reader would."""
    return typ.cast(
        "_BrokenPipeFixture",
        {"sink": _BrokenPipeSink(), "lines": []},
    )


@when("I run an echoed command under the best-effort broken-pipe policy")
def _run_best_effort(
    command_builder: cabc.Callable[..., SafeCmd],
    scenario_state: _BrokenPipeFixture,
) -> None:
    """Run the echoed command, tolerating the closed sink."""
    state = scenario_state
    state["result"] = command_builder("-c", _SCRIPT).run_sync(
        output=_options(BrokenPipePolicy.BEST_EFFORT),
        context=ExecutionContext(
            stdout_sink=typ.cast("typ.IO[str]", state["sink"]),
        ),
    )


@when("I run an observed, echoed command under the best-effort broken-pipe policy")
def _run_best_effort_observed(
    command_builder: cabc.Callable[..., SafeCmd],
    scenario_state: _BrokenPipeFixture,
) -> None:
    """Run with line observation alongside the tolerated echo."""
    state = scenario_state
    state["result"] = command_builder("-c", _SCRIPT).run_sync(
        output=RunOutputOptions(
            capture=True,
            echo_stdout=True,
            broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
            on_line=lambda event: state["lines"].append(event.text),
        ),
        context=ExecutionContext(
            stdout_sink=typ.cast("typ.IO[str]", state["sink"]),
        ),
    )


@when("I run an echoed command under the default policy")
def _run_default(
    command_builder: cabc.Callable[..., SafeCmd],
    scenario_state: _BrokenPipeFixture,
) -> None:
    """Run the echoed command with no policy named, capturing what it raises."""
    state = scenario_state
    try:
        command_builder("-c", _SCRIPT).run_sync(
            output=RunOutputOptions(capture=True, echo_stdout=True),
            context=ExecutionContext(
                stdout_sink=typ.cast("typ.IO[str]", state["sink"]),
            ),
        )
    except BaseException as exc:  # ruff: ignore[BLE001] - the scenario records it
        state["error"] = exc
        return
    msg = "the default policy must propagate the sink's broken pipe"
    raise AssertionError(msg)


@then("the run returns a result with its stdout captured")
def _assert_captured(scenario_state: _BrokenPipeFixture) -> None:
    """Capture is complete and byte-for-byte intact despite the closed sink."""
    state = scenario_state
    assert state["result"].stdout == _EXPECTED_STDOUT, (
        "capture must survive the tolerated echo failure, got "
        f"{state['result'].stdout!r}"
    )


@then("the result records one broken-pipe relay fallback on stdout")
def _assert_fallback(scenario_state: _BrokenPipeFixture) -> None:
    """Exactly one stdout record describes the tolerated transition."""
    state = scenario_state
    assert state["result"].relay_fallbacks == (_EXPECTED_FALLBACK,), (
        "exactly one stdout broken-pipe record is expected, got "
        f"{state['result'].relay_fallbacks!r}"
    )
    assert state["sink"].attempts == [_EXPECTED_STDOUT], (
        "echo must stop after the first broken write, got "
        f"attempts={state['sink'].attempts!r}"
    )


@then("the run raises BrokenPipeError instead of returning a result")
def _assert_propagated(scenario_state: _BrokenPipeFixture) -> None:
    """The default policy leaves the existing contract untouched."""
    error = scenario_state["error"]
    assert isinstance(error, BrokenPipeError), (
        f"the default policy must raise BrokenPipeError, got {error!r}"
    )
    assert _CLOSED_READER in str(error), f"the sink's own error must surface, got {error!r}"


@then("the observed lines are complete despite the closed sink")
def _assert_lines(scenario_state: _BrokenPipeFixture) -> None:
    """Line observation is independent of echo and keeps delivering."""
    lines = scenario_state["lines"]
    assert lines == ["hello"], (
        f"line observation must continue after the echo stops, got {lines!r}"
    )


@then("the run reports the child's own exit status")
def _assert_exit_status(scenario_state: _BrokenPipeFixture) -> None:
    """A tolerated echo failure never changes the child's outcome."""
    result = scenario_state["result"]
    assert result.exit_code == 0, (
        f"the child succeeded, so the result must say so, got {result.exit_code!r}"
    )
    assert result.ok, "a tolerated echo failure must not mark the run failed"


@scenario(
    "../features/broken_pipe_policy.feature",
    "Best-effort echo returns the captured result after the sink closes",
)
def test_best_effort_returns_captured_result() -> None:
    """Behavioural coverage for the opted-in recovery."""


@scenario(
    "../features/broken_pipe_policy.feature",
    "Strict echo still propagates the broken pipe",
)
def test_strict_still_propagates() -> None:
    """Behavioural coverage for the default, unchanged contract."""


@scenario(
    "../features/broken_pipe_policy.feature",
    "Best-effort echo leaves capture, line observation and exit status intact",
)
def test_best_effort_preserves_everything_else() -> None:
    """Behavioural coverage for the parts the policy must not disturb."""
