"""Real-child, externally supervised tests for owned live byte streams.

Each scenario runs in a separate interpreter under
:func:`tests.helpers.supervised_scenario.run_scenario`, which owns the
scenario's session and kills it on a hard deadline. A rescued timeout is
reported as a failure, so a hang can never be mistaken for a pass.

The assertions here are deliberately about observable bytes and closures:
reconstructed per-stream bytes, the captured result, the reader's terminal
state, and the child's recorded outcome. No assertion depends on read sizes or
on timing beyond the supervisor's deadline.
"""

from __future__ import annotations

import asyncio
import os
import sys
import typing as typ

import pytest

from cuprum import (
    ExecutionContext,
    ForbiddenProgramError,
    Program,
    ProgramCatalogue,
    RunOutputOptions,
    ScopeConfig,
    StdinInput,
    StdioTarget,
    scoped,
    sh,
)
from tests.helpers.supervised_scenario import ScenarioResult, run_scenario

if typ.TYPE_CHECKING:
    from cuprum.sh import BytesCommandResult, SafeCmdBuilder

_PORTABLE_SCENARIOS = (
    "prefix_before_input",
    "independent_eof",
    "malformed_split_utf8",
    "large_simultaneous",
    "stalled_then_wait",
    "cancel_while_blocked",
    "close_one_stream",
)

_POSIX_SCENARIOS = (
    "descendant_own_group",
    "descendant_inherit",
)

_CATALOGUE = ProgramCatalogue.from_programs(sys.executable, name="live-bytes")
_PYTHON = sh.make(Program(sys.executable), catalogue=_CATALOGUE)


def _assert_ok(result: ScenarioResult) -> None:
    """Fail with the scenario's captured diagnostics unless it succeeded."""
    assert result.ok, (
        f"scenario {result.name} failed "
        f"(timed_out={result.timed_out}, returncode={result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


@pytest.mark.timeout(25)
@pytest.mark.parametrize("name", _PORTABLE_SCENARIOS)
def test_real_child_scenario_succeeds(name: str) -> None:
    """Every portable live-byte scenario completes within the watchdog."""
    _assert_ok(run_scenario(name))


@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups are required")
@pytest.mark.timeout(25)
@pytest.mark.parametrize("name", _POSIX_SCENARIOS)
def test_posix_descendant_scenario_succeeds(name: str) -> None:
    """The POSIX descendant scenarios settle and clean up within the watchdog."""
    _assert_ok(run_scenario(name))


def test_start_bytes_rejects_a_line_observer_before_spawning() -> None:
    """A byte run cannot carry a caller-supplied line observer."""
    options = RunOutputOptions(on_line=lambda line: None)
    with pytest.raises(ValueError, match="on_line"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(output=options))


def test_start_bytes_rejects_a_redirected_stream() -> None:
    """A handle reads both streams through parent-side pipes."""
    options = RunOutputOptions(capture=False, stdout=StdioTarget.inherit())
    with pytest.raises(ValueError, match="stdout cannot be redirected"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(output=options))


def test_start_bytes_rejects_an_invalid_queue_bound() -> None:
    """The live queue bound is a strictly positive integer."""
    with pytest.raises(ValueError, match="positive"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(max_buffered_bytes=0))
    with pytest.raises(TypeError, match="integer"):
        asyncio.run(_PYTHON("-c", "pass").start_bytes(max_buffered_bytes=True))


def test_start_bytes_enforces_the_allowlist() -> None:
    """A program outside the active scope is refused before a child exists."""
    other = Program("definitely-not-catalogued")
    catalogue = ProgramCatalogue.from_programs(
        Program(sys.executable),
        other,
        name="live-allowed",
    )
    allowed = sh.make(Program(sys.executable), catalogue=catalogue)
    forbidden = sh.make(other, catalogue=catalogue)
    with scoped(ScopeConfig(allowlist=frozenset({Program(sys.executable)}))):
        with pytest.raises(ForbiddenProgramError):
            asyncio.run(forbidden("-c", "pass").start_bytes())
        result = asyncio.run(_start_and_wait(allowed))
    assert result.ok, "the allowed program must still run"


async def _start_and_wait(command: SafeCmdBuilder) -> BytesCommandResult:
    """Start *command* with an empty byte run and return its result."""
    handle = await command("-c", "pass").start_bytes()
    return await handle.wait()


def test_start_bytes_accepts_a_complete_stdin_payload() -> None:
    """``StdinInput`` is accepted and encoded by the execution context."""

    async def scenario() -> None:
        """Write a text payload and reconstruct the echoed bytes."""
        code = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
        async with await _PYTHON("-c", code).start_bytes(
            stdin=StdinInput(text="héllo"),
            context=ExecutionContext(encoding="utf-8"),
        ) as handle:
            received = bytearray()
            async for chunk in handle.stdout:
                received.extend(chunk.data)
            result = await handle.wait()
        assert bytes(received) == "héllo".encode(), (
            f"stdin and stdout must round-trip, got {bytes(received)!r}"
        )
        assert result.stdout == bytes(received), "capture must equal the live bytes"

    asyncio.run(scenario())
