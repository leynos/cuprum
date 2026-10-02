"""Support helpers for the SafeCmd execution runtime behaviour tests.

This module holds plain helper functions shared by the behaviour scenarios
in :mod:`tests.behaviour.test_execution_runtime`. It carries a leading
underscore so pytest does not collect it as a test module.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import typing as typ

import pytest

from cuprum import sh
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


class WorkerCommand(typ.TypedDict):
    """Safe command and PID-file path for one worker fixture.

    Attributes
    ----------
    command : SafeCmd
        Allowlisted command that starts the worker subprocess.
    pid_file : Path
        Path where the worker records its process identifier.
    """

    command: SafeCmd
    pid_file: Path


@dc.dataclass(frozen=True, slots=True)
class _CancellationOptions:
    """Optional controls for cancellation lifecycle scenarios."""

    cancel_grace: float | None = None
    events: list[ExecEvent] | None = None
    repeat_cancellations: int = 0


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
) -> WorkerCommand:
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


async def _repeat_cancellation_during_terminal_cleanup(
    task: asyncio.Task[typ.Any],
    terminal_hook_started: asyncio.Event,
    release_terminal_hook: asyncio.Event,
    options: _CancellationOptions,
) -> None:
    """Interrupt a run repeatedly while its terminal hook is held open."""
    await asyncio.wait_for(terminal_hook_started.wait(), timeout=5.0)
    for _ in range(options.repeat_cancellations):
        task.cancel()
        await asyncio.sleep(0)
    release_terminal_hook.set()


def _cancel_command_with_grace(
    command: SafeCmd,
    pid_file: Path,
    *,
    options: _CancellationOptions | None = None,
) -> int:
    """Cancel a worker and optionally gate its terminal-hook cleanup."""
    run_options = options or _CancellationOptions()

    async def orchestrate() -> int:
        """Run the command as a task, then cancel it after the PID appears."""
        terminal_hook_started = asyncio.Event()
        release_terminal_hook = asyncio.Event()

        async def observe(event: ExecEvent) -> None:
            """Collect events and optionally hold terminal cleanup open."""
            if run_options.events is not None:
                run_options.events.append(event)
            if event.phase == "settled" and run_options.events is not None:
                terminal_hook_started.set()
                await release_terminal_hook.wait()

        grace = (
            run_options.cancel_grace
            if run_options.cancel_grace is not None
            else ExecutionContext().cancel_grace
        )
        observation_context = (
            sh.observe(observe)
            if run_options.events is not None
            else contextlib.nullcontext()
        )
        task: asyncio.Task[typ.Any] | None = None
        with observation_context:
            task = asyncio.create_task(
                command.run(
                    output=RunOutputOptions(capture=False),
                    context=ExecutionContext(
                        env={"CUPRUM_PID_FILE": str(pid_file)},
                        cancel_grace=grace,
                    ),
                )
            )
            try:
                pid = await _wait_for_pid(pid_file)
            except BaseException:
                release_terminal_hook.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                raise
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                if run_options.events is not None:
                    await _repeat_cancellation_during_terminal_cleanup(
                        task,
                        terminal_hook_started,
                        release_terminal_hook,
                        run_options,
                    )
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release_terminal_hook.set()
            return pid

    return asyncio.run(orchestrate())
