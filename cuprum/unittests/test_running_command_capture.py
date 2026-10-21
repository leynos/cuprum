"""Capture guarantees for commands started through an owned handle."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, ScopeConfig, StdioTarget, scoped, sh
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import CommandResult


@pytest.mark.parametrize("capture", [False, True])
def test_start_captures_streams_with_per_stream_opt_outs(capture: bool) -> None:
    """A handle captures both streams even when each stream field is false."""
    catalogue, python_program = python_catalogue()
    builder = sh.make(python_program, catalogue=catalogue)
    command = builder(
        "-c",
        "import sys; print('stdout'); print('stderr', file=sys.stderr)",
    )

    async def run_handle() -> CommandResult:
        """Start and settle the child under a scope that permits it."""
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            handle = await command.start(
                output=RunOutputOptions(
                    capture=capture,
                    capture_stdout=False,
                    capture_stderr=False,
                ),
            )
            return await handle.wait()

    result = asyncio.run(run_handle())

    assert result.stdout == "stdout\n", (
        f"a started handle must retain stdout despite capture={capture}, "
        f"got {result.stdout!r}"
    )
    assert result.stderr == "stderr\n", (
        f"a started handle must retain stderr despite capture={capture}, "
        f"got {result.stderr!r}"
    )


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_start_rejects_redirected_streams_with_actionable_error(
    stream: typ.Literal["stdout", "stderr"],
    tmp_path: Path,
) -> None:
    """Forced capture refuses redirects with guidance that applies to start."""
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)("-c", "pass")
    target = StdioTarget.path(tmp_path / f"{stream}.log")
    match stream:
        case "stdout":
            output = RunOutputOptions(
                capture=False,
                capture_stdout=False,
                stdout=target,
            )
        case "stderr":
            output = RunOutputOptions(
                capture=False,
                capture_stderr=False,
                stderr=target,
            )

    async def start() -> None:
        """Run the pre-spawn check under the command's allowlist."""
        with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
            await command.start(output=output)

    with pytest.raises(
        ValueError,
        match=(
            rf"SafeCmd\.start\(\) always captures output, so {stream} "
            r"cannot be redirected\."
        ),
    ) as error:
        asyncio.run(start())

    assert str(error.value) == (
        f"SafeCmd.start() always captures output, so {stream} cannot be redirected. "
        "Use run() or run_sync() to redirect streams."
    ), f"the refusal must explain start's forced capture, got {error.value!r}"
