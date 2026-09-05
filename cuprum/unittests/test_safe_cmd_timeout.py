"""Unit tests for SafeCmd timeout and cancellation behaviour.

Covers the public-boundary timeout contract and cancellation teardown on
``run()``/``run_sync()``: ``TimeoutExpired`` and its payload, escalation of
non-cooperative children to ``SIGKILL``, and the absence of stranded tasks or
surviving children after a timeout.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import sys
import typing as typ

import pytest

from cuprum import Program, TimeoutExpired, sh
from cuprum.sh import CommandResult, ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.catalogue import python_catalogue
from tests.helpers.execution import ExecuteFn, _RunKwargs
from tests.helpers.timeouts import (
    child_argv,
    pending_tasks,
    python_interpreter,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent
    from cuprum.sh import SafeCmd


def _execute_async(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the async run() method."""
    return asyncio.run(cmd.run(**kwargs))


def _execute_sync(cmd: SafeCmd, kwargs: _RunKwargs) -> CommandResult:
    """Execute a SafeCmd using the sync run_sync() method."""
    return cmd.run_sync(**kwargs)


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def execution_strategy(request: pytest.FixtureRequest) -> tuple[str, ExecuteFn]:
    """Provide parameterized execution strategies for run() and run_sync().

    Returns
    -------
    tuple[str, ExecuteFn]
        The strategy label and its execution callable.
    """
    if request.param == "async":
        return ("async", _execute_async)
    return ("sync", _execute_sync)


@pytest.fixture
def python_builder() -> cabc.Callable[..., SafeCmd]:
    """Provide a SafeCmd builder for the current Python interpreter.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that creates SafeCmd instances for the running interpreter.
    """
    return build_python_builder()


def test_timeout_raises_timeout_expired(
    python_builder: cabc.Callable[..., SafeCmd],
    execution_strategy: tuple[str, ExecuteFn],
) -> None:
    """Timeouts raise TimeoutExpired with no output when capture is disabled."""
    _, execute = execution_strategy
    command = python_builder("-c", "import time; time.sleep(2)")

    with pytest.raises(TimeoutExpired, match=r"timed out") as exc_info:
        execute(
            command,
            {"timeout": 0.1, "output": RunOutputOptions(capture=False)},
        )

    assert exc_info.value.timeout == pytest.approx(0.1)
    assert exc_info.value.stdout is None
    assert exc_info.value.stderr is None


# -----------------------------------------------------------------------------
# Async-only tests (cancellation semantics)
# -----------------------------------------------------------------------------


def test_non_cooperative_subprocess_is_escalated_and_killed(
    tmp_path: Path,
    python_builder: cabc.Callable[..., SafeCmd],
) -> None:
    """Non-cooperative child is killed after cancel_grace elapses."""
    if sys.platform == "win32":  # pragma: no cover - platform-specific behaviour
        pytest.skip("Cancellation escalation semantics rely on POSIX signals")
    script = tmp_path / "non_cooperative_child.py"
    pid_file = tmp_path / "nc.pid"
    script.write_text(
        "\n".join(
            (
                "import os",
                "import pathlib",
                "import signal",
                "import time",
                "pid_file = pathlib.Path(os.environ['CUPRUM_PID_FILE'])",
                "pid_file.write_text(str(os.getpid()))",
                "def _ignore(_signum, _frame):",
                "    pass",
                "signal.signal(signal.SIGTERM, _ignore)",
                "signal.signal(signal.SIGINT, _ignore)",
                "while True:",
                "    time.sleep(0.1)",
            ),
        ),
        encoding="utf-8",
    )

    command = python_builder(str(script))

    async def orchestrate() -> int:
        """Run the child, cancel it, and return its recorded PID."""
        task = asyncio.create_task(
            command.run(
                output=RunOutputOptions(capture=False),
                context=ExecutionContext(
                    env={"CUPRUM_PID_FILE": str(pid_file)},
                    cancel_grace=0.1,
                ),
            ),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while loop.time() < deadline:
            if pid_file.exists():
                break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - defensive guard for CI slowness
            pytest.fail("PID file was not created within 5s")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return int(pid_file.read_text())

    pid = asyncio.run(orchestrate())
    wait_for_process_death(pid, seconds=1.0, context="cancellation")


# -- Public-boundary timeout contract -----------------------------------------
#
# The private-helper tests in ``test_subprocess_timeout`` drive
# ``_wait_for_exit_code_within_timeout`` with process doubles, which isolates
# branches a public test cannot reach. These cover what a caller actually sees:
# the public ``TimeoutExpired``, its payload, and the absence of any surviving
# child or stranded task.


type TimeoutRunFn = cabc.Callable[
    [SafeCmd, _RunKwargs], tuple[TimeoutExpired, set[asyncio.Task[object]]]
]


def _timeout_async(
    cmd: SafeCmd, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run()``, returning the timeout and any tasks left pending."""

    async def run_case() -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
        """Await the run inside a loop that is still open for inspection."""
        with pytest.raises(TimeoutExpired) as exc_info:
            await cmd.run(**kwargs)
        return exc_info.value, pending_tasks()

    return asyncio.run(run_case())


def _timeout_sync(
    cmd: SafeCmd, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run_sync()``; its loop is closed before control returns.

    ``run_sync`` owns and closes its event loop, so no task can outlive the
    call and there is nothing left to inspect. The leak assertion has its teeth
    in the ``run()`` variant; here the surviving-child assertion carries the
    cleanup contract.

    Returns
    -------
    tuple[TimeoutExpired, set[asyncio.Task[object]]]
        The raised timeout and an always-empty task set.
    """
    with pytest.raises(TimeoutExpired) as exc_info:
        cmd.run_sync(**kwargs)
    return exc_info.value, set()


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def timeout_execution_strategy(request: pytest.FixtureRequest) -> TimeoutRunFn:
    """Provide run()/run_sync() strategies that expect a timeout."""
    return _timeout_async if request.param == "async" else _timeout_sync


@pytest.mark.parametrize("configured_timeout", [0, -1.0])
@pytest.mark.parametrize("capture", [True, False])
def test_non_positive_timeout_at_public_boundary(
    configured_timeout: float,
    *,
    capture: bool,
    timeout_execution_strategy: TimeoutRunFn,
    tmp_path: Path,
) -> None:
    """A non-positive timeout expires immediately through the public API.

    A non-positive deadline is already elapsed, so expiry is structural: the
    child blocks forever and only the timeout can end the run, with no reliance
    on elapsed wall-clock time. Asserts the public exception and its payload,
    that no child survives, and that nothing is left pending on the loop.
    """
    command = sh.make(Program(python_interpreter()), catalogue=python_catalogue()[0])(
        *child_argv(tmp_path / "ready")
    )
    events: list[ExecEvent] = []

    with sh.observe(events.append):
        expired, leaked = timeout_execution_strategy(
            command,
            {
                "timeout": configured_timeout,
                "output": RunOutputOptions(capture=capture),
            },
        )

    assert expired.timeout == configured_timeout, (
        f"TimeoutExpired must preserve the configured timeout "
        f"{configured_timeout!r}, got {expired.timeout!r}"
    )
    assert not leaked, f"the run left pending tasks behind: {leaked!r}"

    pids = started_pids(events)
    assert len(pids) == 1, f"expected exactly one spawned child, got {pids!r}"
    wait_for_process_death(pids[0], seconds=1.0, context="the timeout")

    detail = f"output={expired.output!r} stderr={expired.stderr!r}"
    if capture:
        assert isinstance(expired.output, str), (
            f"a capturing run must surface partial stdout as a string, got {detail}"
        )
        assert isinstance(expired.stderr, str), (
            f"a capturing run must surface partial stderr as a string, got {detail}"
        )
    else:
        assert expired.output is None, (
            f"a non-capturing run must leave stdout unset, got {detail}"
        )
        assert expired.stderr is None, (
            f"a non-capturing run must leave stderr unset, got {detail}"
        )
