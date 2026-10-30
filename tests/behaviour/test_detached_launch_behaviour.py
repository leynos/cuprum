"""Behavioural coverage for detached command launch.

The scenarios describe the contract a caller relies on: an allowed command is
launched and its lifetime transferred, and every refusal — a forbidden program,
an unsupported platform, an executable that cannot be started — happens before
a worker exists. The last scenario covers the one place a cancellation can
still leave an orphan: the yield between the spawn and the receipt, where
cuprum still owns the worker because the caller never received it.

The spawn is replaced by a recorder in most scenarios, because a behaviour test
here is about which decisions the launch makes and in what order. The two
start-up failures are the exception: they keep the real spawn, since the point
is that ``Popen``'s own exec failure surfaces instead of a receipt. Child
process behaviour is covered separately by the cross-process harness in
``test_detached_launch_harness``.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses as dc
import os
import typing as typ

import pytest
from pytest_bdd import given, scenario, then, when

from cuprum import ECHO, ForbiddenProgramError, ScopeConfig, bind_executable, scoped
from cuprum._detached_spawn import _spawn_detached
from cuprum.sh import ExecutionContext
from tests.helpers.catalogue import build_python_catalogue_env
from tests.helpers.detached import (
    FAKE_PID,
    FakeProcess,
    SpawnCall,
    record_abandonment,
    record_spawns,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.program import Program
    from cuprum.sh import DetachedLaunch, SafeCmd
    from tests.helpers.catalogue import PythonCatalogue

# A detached launch requires POSIX session isolation, and one scenario asserts
# the refusal a non-POSIX platform receives, so both halves run wherever the
# feature exists.
pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="a detached launch requires POSIX session isolation",
)


@dc.dataclass
class LaunchScenario:
    """The state one scenario builds up across its steps."""

    env: PythonCatalogue
    calls: list[SpawnCall]
    abandoned: list[int]
    builder: cabc.Callable[..., SafeCmd] | None = None
    program: Program | None = None
    allowed: Program | None = None
    bind_to: Path | None = None
    cancels_at_spawn: bool = False
    receipt: DetachedLaunch | None = None
    error: BaseException | None = None


@pytest.fixture
def launch_scenario(monkeypatch: pytest.MonkeyPatch) -> LaunchScenario:
    """Provide scenario state with the spawn replaced by a recorder."""
    return LaunchScenario(
        env=build_python_catalogue_env(),
        calls=record_spawns(monkeypatch),
        abandoned=record_abandonment(monkeypatch),
    )


@given("an allowlisted command for the current interpreter")
def _allowed_command(launch_scenario: LaunchScenario) -> None:
    """Adopt the interpreter catalogue's program and command builder."""
    launch_scenario.builder = launch_scenario.env.builder
    launch_scenario.program = launch_scenario.env.program


@given("a scope that allows a different program")
def _scope_allows_another(launch_scenario: LaunchScenario) -> None:
    """Narrow the allowlist to a program the command is not."""
    launch_scenario.allowed = ECHO


@given("the host reports a platform without POSIX sessions")
def _platform_without_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report a platform that cannot provide session isolation."""
    monkeypatch.setattr(os, "name", "nt")


@given("the program is bound to a file that does not exist")
def _bind_to_absent_file(
    launch_scenario: LaunchScenario,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bind the program to a path nothing can execute and keep the real spawn."""
    launch_scenario.bind_to = tmp_path / "absent-tool"
    # The start-up failure belongs to ``Popen``, so this scenario must not
    # record the spawn: it has to reach the seam and come back with an error.
    monkeypatch.setattr(
        "cuprum._detached_launch._spawn_detached",
        _spawn_detached,
    )


@given("a launch that is cancelled as it spawns")
def _cancel_at_spawn(
    launch_scenario: LaunchScenario,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Request cancellation from inside the spawn of the launch."""
    launch_scenario.cancels_at_spawn = True

    def cancelling_spawn(argv: cabc.Sequence[str], **_: object) -> FakeProcess:
        """Cancel the launching task and report a PID."""
        task = asyncio.current_task()
        assert task is not None, "the launch must run inside a task"
        task.cancel()
        return FakeProcess()

    monkeypatch.setattr(
        "cuprum._detached_launch._spawn_detached",
        cancelling_spawn,
    )


@when("the command is launched detached")
def _launch_detached(launch_scenario: LaunchScenario) -> None:
    """Launch the command, recording the receipt or the error it raised."""
    builder = launch_scenario.builder
    program = launch_scenario.program
    assert builder is not None, "the Given steps must establish a command builder"
    assert program is not None, "the Given steps must establish a program"
    command = builder("-c", "pass")
    if launch_scenario.cancels_at_spawn:
        _launch_and_cancel(launch_scenario, command)
        return
    with _scope_around(launch_scenario, launch_scenario.allowed or program):
        _launch_and_record(launch_scenario, command)


@then("the receipt names the worker that was started")
def _receipt_names_worker(launch_scenario: LaunchScenario) -> None:
    """Assert the receipt names the spawned worker."""
    assert launch_scenario.error is None, (
        f"the launch failed unexpectedly: {launch_scenario.error!r}"
    )
    receipt = launch_scenario.receipt
    assert receipt is not None, "an allowed command must produce a receipt"
    assert receipt.pid == FAKE_PID, (
        f"the receipt must name the spawned worker, got {receipt.pid}"
    )


@then("the launch fails with a permission error")
def _fails_with_permission_error(launch_scenario: LaunchScenario) -> None:
    """Assert the allowlist refused the launch."""
    assert isinstance(launch_scenario.error, ForbiddenProgramError), (
        f"expected a permission error, got {launch_scenario.error!r}"
    )


@then("the launch fails with a value error")
def _fails_with_value_error(launch_scenario: LaunchScenario) -> None:
    """Assert the platform was refused before any spawn."""
    assert isinstance(launch_scenario.error, ValueError), (
        f"expected a value error, got {launch_scenario.error!r}"
    )


@then("the launch fails with a file-not-found error")
def _fails_with_file_not_found(launch_scenario: LaunchScenario) -> None:
    """Assert the executable could not be started."""
    assert isinstance(launch_scenario.error, FileNotFoundError), (
        f"expected a start-up failure, got {launch_scenario.error!r}"
    )


@then("no worker was started")
def _no_worker_started(launch_scenario: LaunchScenario) -> None:
    """Assert the refusal preceded the spawn."""
    assert not launch_scenario.calls, (
        f"a refused launch started a worker: {launch_scenario.calls!r}"
    )


@then("no receipt is returned")
def _no_receipt(launch_scenario: LaunchScenario) -> None:
    """Assert the failed launch reported no PID."""
    assert launch_scenario.receipt is None, (
        f"a failed launch returned a receipt: {launch_scenario.receipt!r}"
    )


@then("the launch is cancelled")
def _launch_cancelled(launch_scenario: LaunchScenario) -> None:
    """Assert the cancellation reached the caller."""
    assert isinstance(launch_scenario.error, asyncio.CancelledError), (
        f"expected a cancellation, got {launch_scenario.error!r}"
    )
    assert launch_scenario.receipt is None, "a cancelled launch must hand back nothing"


@then("the orphaned worker is stopped")
def _orphan_stopped(launch_scenario: LaunchScenario) -> None:
    """Assert cuprum ended the worker the caller never received."""
    assert launch_scenario.abandoned == [FAKE_PID], (
        f"the orphaned worker was not stopped: {launch_scenario.abandoned!r}"
    )


@scenario(
    "../features/detached_launch.feature",
    "An allowed program is launched and its lifetime transferred",
)
def test_allowed_program_is_launched() -> None:
    """An allowed command produces a receipt naming its worker."""


@scenario(
    "../features/detached_launch.feature",
    "A forbidden program is refused before a worker exists",
)
def test_forbidden_program_is_refused() -> None:
    """A program outside the allowlist never reaches the spawn."""


@scenario(
    "../features/detached_launch.feature",
    "A platform without POSIX sessions is refused before a worker exists",
)
def test_unsupported_platform_is_refused() -> None:
    """A platform without session isolation never reaches the spawn."""


@scenario(
    "../features/detached_launch.feature",
    "A missing executable fails the launch instead of returning a receipt",
)
def test_missing_executable_fails_the_launch() -> None:
    """An unstartable executable surfaces as an error, not a receipt."""


@scenario(
    "../features/detached_launch.feature",
    "Cancelling at the transfer boundary stops the worker",
)
def test_cancellation_stops_the_orphan() -> None:
    """A cancellation before the receipt ends the worker it would have named."""


def _launch_and_record(launch_scenario: LaunchScenario, command: SafeCmd) -> None:
    """Perform the launch, recording its receipt or the error it raised."""
    try:
        launch_scenario.receipt = command.launch_detached_sync(
            context=ExecutionContext()
        )
    except (ForbiddenProgramError, ValueError, OSError) as error:
        launch_scenario.error = error


def _launch_and_cancel(launch_scenario: LaunchScenario, command: SafeCmd) -> None:
    """Perform the launch in a task cancelled from inside the spawn."""

    async def run() -> None:
        """Run the launch and keep its receipt on the scenario."""
        launch_scenario.receipt = await command.launch_detached()

    try:
        asyncio.run(run())
    except asyncio.CancelledError as error:
        launch_scenario.error = error


def _scope_around(
    launch_scenario: LaunchScenario,
    allowed: Program,
) -> contextlib.ExitStack:
    """Enter the allowlist scope, and the executable binding when one applies."""
    stack = contextlib.ExitStack()
    stack.enter_context(scoped(ScopeConfig(allowlist=frozenset({allowed}))))
    if launch_scenario.bind_to is not None:
        program = launch_scenario.program
        assert program is not None, "a binding needs the program it binds"
        stack.enter_context(bind_executable(program, str(launch_scenario.bind_to)))
    return stack
