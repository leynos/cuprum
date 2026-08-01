"""Support helpers for the SafeCmd execution runtime behaviour tests.

This module holds plain helper functions shared by the behaviour scenarios
in :mod:`tests.behaviour.test_execution_runtime`. It carries a leading
underscore so pytest does not collect it as a test module.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import sh
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.sh import SafeCmd

__all__ = [
    "_cancel_command_with_grace",
    "_create_worker_command",
    "_wait_for_pid",
]


def _create_worker_command(
    tmp_path: Path,
    *,
    script_name: str,
    cooperative: bool = True,
) -> dict[str, object]:
    """Create a SafeCmd-backed worker script."""
    script_path = tmp_path / script_name
    pid_file = tmp_path / f"{script_path.stem}.pid"
    signal_handler_body = (
        (
            "def _stop(_signum, _frame):",
            "    sys.exit(0)",
            "signal.signal(signal.SIGTERM, _stop)",
            "signal.signal(signal.SIGINT, _stop)",
        )
        if cooperative
        else (
            "def _ignore(_signum, _frame):",
            "    pass",
            "signal.signal(signal.SIGTERM, _ignore)",
            "signal.signal(signal.SIGINT, _ignore)",
        )
    )
    script_path.write_text(
        "\n".join(
            (
                "import os",
                "import pathlib",
                "import signal",
                "import sys",
                "import time",
                "pid_file = pathlib.Path(os.environ['CUPRUM_PID_FILE'])",
                "pid_tmp = pid_file.with_name(pid_file.name + '.tmp')",
                "pid_tmp.write_text(str(os.getpid()))",
                "pid_tmp.replace(pid_file)",
                *signal_handler_body,
                "while True:",
                "    time.sleep(1)",
            ),
        ),
        encoding="utf-8",
    )
    catalogue, python_program = python_catalogue()
    command = sh.make(python_program, catalogue=catalogue)(str(script_path))
    return {"command": command, "pid_file": pid_file}


async def _wait_for_pid(pid_file: Path, timeout: float = 5.0) -> int:
    """Wait for the child process to publish its PID."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pid_file.exists():
            return int(pid_file.read_text().strip())
        await asyncio.sleep(0.05)
    msg = f"PID file was not created within {timeout}s"
    raise TimeoutError(msg)


def _cancel_command_with_grace(
    command: SafeCmd,
    pid_file: Path,
    *,
    cancel_grace: float | None = None,
) -> int:
    """Run a command, cancel it, and return the recorded child PID."""

    async def orchestrate() -> int:
        grace = (
            cancel_grace
            if cancel_grace is not None
            else ExecutionContext().cancel_grace
        )
        task = asyncio.create_task(
            command.run(
                output=RunOutputOptions(capture=False),
                context=ExecutionContext(
                    env={"CUPRUM_PID_FILE": str(pid_file)},
                    cancel_grace=grace,
                ),
            ),
        )
        pid = await _wait_for_pid(pid_file)
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pid

    return asyncio.run(orchestrate())
