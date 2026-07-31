"""Async observe-hook failures must not mask the error that ended a run.

A hook returning an awaitable is scheduled as a background task, so its failure
surfaces later — while cleanup drains the pending tasks — rather than at emit
time. ``_safe_emit`` can only swallow a *synchronous* hook failure, so these
tests cover the async case: the drain must aggregate the hook failure with the
active error instead of replacing it. The synchronous counterpart lives in
``test_observe``.
"""

from __future__ import annotations

import asyncio
import sys
import typing as typ
from pathlib import Path

import pytest

from cuprum import sh
from cuprum.catalogue import ProgramCatalogue, ProjectSettings
from cuprum.context import ScopeConfig, scoped
from cuprum.program import Program

if typ.TYPE_CHECKING:
    from cuprum.events import ExecEvent


class _ObserveTaskError(RuntimeError):
    """Raised by a deliberately failing async observe hook."""


def _sleep_command() -> tuple[sh.SafeCmd, ProgramCatalogue]:
    """Build a long-sleeping Python command and its allowlist catalogue."""
    python_program = Program(str(Path(sys.executable)))
    project = ProjectSettings(
        name="async-hook-failure",
        programs=(python_program,),
        documentation_locations=("docs/users-guide.md",),
        noise_rules=(),
    )
    catalogue = ProgramCatalogue(projects=(project,))
    builder = sh.make(python_program, catalogue=catalogue)
    return builder("-c", "import time; time.sleep(30)"), catalogue


def _assert_aggregates(
    group: BaseExceptionGroup[BaseException],
    primary: type[BaseException],
) -> None:
    """Assert the group preserves ``primary`` alongside the hook failure."""
    types = tuple(type(error) for error in group.exceptions)
    assert any(isinstance(error, primary) for error in group.exceptions), (
        f"cleanup must preserve the {primary.__name__} that ended the run "
        f"rather than replacing it with the hook failure, got {types}"
    )
    assert any(isinstance(error, _ObserveTaskError) for error in group.exceptions), (
        f"cleanup must report the failing observe task, got {types}"
    )


def test_async_hook_failure_on_timeout_preserves_timeout_expired() -> None:
    """A failing async observe task cannot replace ``TimeoutExpired``.

    ``timeout=0`` takes the deterministic immediate-expiry path, so the run is
    already unwinding with ``TimeoutExpired`` when the drain reaches the failed
    hook task. The drain must aggregate the two rather than let the hook's
    error stand in for the timeout a caller is waiting to catch.
    """
    cmd, catalogue = _sleep_command()

    async def hook(ev: ExecEvent) -> None:
        """Fail asynchronously once the timeout event has been emitted."""
        if ev.phase == "timeout":
            await asyncio.sleep(0)
            raise _ObserveTaskError

    with (
        scoped(ScopeConfig(allowlist=catalogue.allowlist)),
        sh.observe(hook),
        pytest.raises(BaseExceptionGroup) as exc_info,
    ):
        cmd.run_sync(timeout=0)

    _assert_aggregates(exc_info.value, sh.TimeoutExpired)


def test_async_hook_failure_on_cancellation_preserves_cancelled_error() -> None:
    """A failing async observe task cannot replace ``CancelledError``.

    The hook fails on the ``start`` event, so its task is already broken when
    the run is cancelled and cleanup drains it. Cancellation must still reach
    the caller, aggregated with the hook failure.
    """
    cmd, catalogue = _sleep_command()

    async def run_case() -> None:
        """Cancel a running command whose observe task has already failed."""
        started = asyncio.Event()

        async def hook(ev: ExecEvent) -> None:
            """Signal readiness on start, then fail as a background task."""
            if ev.phase == "start":
                started.set()
                await asyncio.sleep(0)
                raise _ObserveTaskError

        with scoped(ScopeConfig(allowlist=catalogue.allowlist)), sh.observe(hook):
            task = asyncio.create_task(cmd.run())
            # Wait for the real start event rather than guessing at a delay, so
            # the cancellation lands once the hook task exists.
            await asyncio.wait_for(started.wait(), timeout=10.0)
            task.cancel()

            with pytest.raises(BaseExceptionGroup) as exc_info:
                await task

        _assert_aggregates(exc_info.value, asyncio.CancelledError)

    asyncio.run(run_case())
