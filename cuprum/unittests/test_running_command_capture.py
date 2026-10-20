"""Capture guarantees for commands started through an owned handle."""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, ScopeConfig, scoped, sh
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
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
