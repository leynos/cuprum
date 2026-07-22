"""Integration tests for the SafeCmd stdin writer lifecycle.

Covers stdin-fed executions where the writer task must be cleaned up on
timeout escalation, on a blocked ``drain()`` in direct mode, and on
cancellation, across both the ``run()`` and ``run_sync()`` strategies.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import typing as typ

import pytest

from cuprum import TimeoutExpired
from cuprum.sh import CommandResult, RunOutputOptions, StdinInput
from tests.helpers.catalogue import python_builder as build_python_builder

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.sh import SafeCmd


def _execute_async(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: dict[str, typ.Any]) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter."""
    return build_python_builder()


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_stdin_input_with_timeout_escalation(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: str,
) -> None:
    """Stdin writer task is cleaned up when the command times out."""
    command = python_builder(
        "-c",
        "import sys, time; sys.stdin.read(); time.sleep(10)",
    )
    execute = _execute_async if execution_strategy == "async" else _execute_sync
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(text="x"),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=False),
            },
        )


@pytest.mark.parametrize("execution_strategy", ["async", "sync"])
def test_direct_timeout_with_blocked_stdin_writer_does_not_hang(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: str,
) -> None:
    """Direct-mode timeout cancels a stdin writer wedged on an unread pipe.

    The child never reads its stdin, so a payload larger than the OS pipe
    buffer stalls the writer's ``drain()``. On timeout the runner must cancel
    that writer rather than wait on it, raising ``TimeoutExpired`` promptly
    instead of blocking on the stalled drain (regression for #117).
    """
    command = python_builder("-c", "import time; time.sleep(3600)")
    execute = _execute_async if execution_strategy == "async" else _execute_sync
    payload = b"x" * (1024 * 1024)  # 1 MiB dwarfs the ~64 KiB pipe buffer
    started = time.perf_counter()
    with pytest.raises(TimeoutExpired):
        execute(
            command,
            {
                "stdin": StdinInput(data=payload),
                "timeout": 0.2,
                "output": RunOutputOptions(capture=False),
            },
        )
    elapsed = time.perf_counter() - started
    assert elapsed < 10.0, (
        "direct-mode timeout with a blocked stdin writer must not hang; "
        f"took {elapsed:.2f}s"
    )


def test_stdin_input_cancellation_cleans_up_task(
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Cancelling a command with stdin data does not hang."""

    async def _orchestrate() -> None:
        """Start a stdin-fed command and cancel it without hanging."""
        command = python_builder(
            "-c",
            "import sys, time; sys.stdin.read(); time.sleep(30)",
        )
        task = asyncio.create_task(
            command.run(
                output=RunOutputOptions(capture=False),
                stdin=StdinInput(text="payload"),
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=2.0)

    asyncio.run(_orchestrate())
