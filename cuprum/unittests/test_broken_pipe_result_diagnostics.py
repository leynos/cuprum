"""Result-level coverage for the opt-in broken-pipe policy (#435).

The drain-level contract lives in ``test_broken_pipe_echo_guard.py`` and the
sibling encoding-failure result contract in
``test_relay_fallback_diagnostics.py`` (which sits at the module line ceiling,
hence this sibling). This module pins the path from a caller's
``RunOutputOptions.broken_pipe_policy`` through the execution bundle to the
``CommandResult``: under ``BEST_EFFORT`` a run whose presentation sink has
closed returns a result carrying one ``BROKEN_PIPE`` record, and under the
default the same run still raises.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import BrokenPipePolicy, RelayFallback
from cuprum.echo_events import EchoErrorCategory, EchoStream
from cuprum.echo_observation import observe_echo
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


_EXPECTED_STDOUT_FALLBACK = RelayFallback(
    stream=EchoStream.STDOUT,
    error_category=EchoErrorCategory.BROKEN_PIPE,
)
_CLOSED_READER = "closed presentation destination"


class _BrokenPipeSink:
    """Text-only sink whose destination has closed under the run."""

    def __init__(self) -> None:
        """Record each attempted write before failing."""
        self.attempts: list[str] = []

    def write(self, payload: str) -> int:
        """Record the attempt, then fail the way a closed reader does."""
        self.attempts.append(payload)
        raise BrokenPipeError(_CLOSED_READER)

    def flush(self) -> None:
        """Model the flush call on a broken stream."""


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


def test_best_effort_returns_a_result_when_stdout_sink_pipe_is_broken(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The ticket's reproduction returns a result instead of raising."""
    sink = _BrokenPipeSink()

    async def run_case() -> CommandResult:
        """Echo stdout into a closed destination under BEST_EFFORT."""
        with observe_echo(lambda _event: None):
            return await python_builder("-c", "print('hello')").run(
                output=RunOutputOptions(
                    capture=True,
                    echo_stdout=True,
                    broken_pipe_policy=BrokenPipePolicy.BEST_EFFORT,
                ),
                context=ExecutionContext(stdout_sink=typ.cast("typ.IO[str]", sink)),
            )

    result = asyncio.run(run_case())

    assert result.stdout == "hello\n", (
        f"capture must be complete and byte-for-byte intact, got {result.stdout!r}"
    )
    assert result.ok, "a tolerated echo failure must not change the exit status"
    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        "exactly one stdout BROKEN_PIPE record is expected, got "
        f"{result.relay_fallbacks!r}"
    )
    assert sink.attempts == ["hello\n"], (
        f"echo must stop after the first broken write, got attempts={sink.attempts!r}"
    )


def test_best_effort_accepts_the_string_spelling_of_the_policy(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """A caller may name the policy by its value rather than its member."""
    sink = _BrokenPipeSink()

    async def run_case() -> CommandResult:
        """Opt in with the raw string the enum parses from."""
        with observe_echo(lambda _event: None):
            return await python_builder("-c", "print('hello')").run(
                output=RunOutputOptions(
                    capture=True,
                    echo_stdout=True,
                    broken_pipe_policy="best_effort",
                ),
                context=ExecutionContext(stdout_sink=typ.cast("typ.IO[str]", sink)),
            )

    result = asyncio.run(run_case())

    assert result.relay_fallbacks == (_EXPECTED_STDOUT_FALLBACK,), (
        "the string spelling must reach the drain as the member, got "
        f"{result.relay_fallbacks!r}"
    )


def test_default_policy_still_raises_when_the_stdout_sink_pipe_is_broken(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """The negative control: without opting in, the run still aborts."""
    sink = _BrokenPipeSink()

    async def run_case() -> CommandResult:
        """Echo stdout into a closed destination under the default policy."""
        with observe_echo(lambda _event: None):
            return await python_builder("-c", "print('hello')").run(
                output=RunOutputOptions(capture=True, echo_stdout=True),
                context=ExecutionContext(stdout_sink=typ.cast("typ.IO[str]", sink)),
            )

    with pytest.raises(BrokenPipeError, match=_CLOSED_READER):
        asyncio.run(run_case())


def test_rejected_policy_value_fails_before_the_child_spawns(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """An unknown policy is a configuration error, not a runtime one."""
    with pytest.raises(ValueError, match="invalid broken_pipe_policy"):
        RunOutputOptions(broken_pipe_policy="wishful_thinking")
