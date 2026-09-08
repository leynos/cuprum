"""Behavioural coverage for stream read-size line boundaries."""

from __future__ import annotations

import typing as typ

from pytest_bdd import given, scenario, then, when

from cuprum import ScopeConfig, scoped, sh
from cuprum._streams_pump import _READ_SIZE
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent
    from cuprum.program import Program
    from cuprum.sh import SafeCmd


@scenario(
    "../features/stream_parity.feature",
    "CRLF split at a read boundary emits no empty line",
)
def test_crlf_boundary_line_emission(stream_backend: str) -> None:
    """CRLF line emission remains correct across a stream read boundary.

    Parameters
    ----------
    stream_backend : str
        The active stream backend (injected by fixture).
    """


@given(
    "a command whose CRLF crosses the stream read boundary",
    target_fixture="observed_command",
)
def given_crlf_boundary_command() -> tuple[SafeCmd, frozenset[Program], str]:
    """Build a command that places CR at the end of the first read."""
    catalogue, python_prog = python_catalogue()
    cmd = sh.make(python_prog, catalogue=catalogue)
    first_line = "a" + "x" * (_READ_SIZE - 2)
    script = f"import sys; sys.stdout.write({first_line!r} + '\\r\\n' + 'b')"
    return cmd("-c", script), frozenset([python_prog]), first_line


@when(
    "I run the command with a line observer",
    target_fixture="observed_lines",
)
def when_run_with_line_observer(
    observed_command: tuple[SafeCmd, frozenset[Program], str],
) -> list[ExecEvent]:
    """Run the boundary command while collecting stdout line events."""
    cmd, allowlist, _first_line = observed_command
    events: list[ExecEvent] = []
    with scoped(ScopeConfig(allowlist=allowlist)), sh.observe(events.append):
        _ = cmd.run_sync()
    return events


@then("the observer receives the complete CRLF line and no empty line")
def then_observer_sees_complete_crlf_line(
    observed_command: tuple[SafeCmd, frozenset[Program], str],
    observed_lines: list[ExecEvent],
) -> None:
    """Assert that CRLF boundary handling emits only the two real lines."""
    _cmd, _allowlist, first_line = observed_command
    stdout_lines = [event.line for event in observed_lines if event.phase == "stdout"]

    assert stdout_lines == [first_line, "b"], (
        "CRLF split across a read boundary must not emit an empty stdout line"
    )
