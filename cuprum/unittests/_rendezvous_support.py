"""Doubles shared by the exit-rendezvous tests.

The race is exercised without a child at all, so every test that pins it needs
the same three things: a callback-shaped failure to race, an exit wait whose
settlement and cancellation the test controls, and tasks that fail on demand.
They live here because more than one module pins the race — the basic outcomes
in ``test_subprocess_rendezvous`` and the window between a writer's raise and
its task settling in ``test_rendezvous_writer_handoff`` — and two copies of a
probe would drift on exactly the ordering the second module depends on.
"""

from __future__ import annotations

import asyncio
import typing as typ

from cuprum._subprocess_rendezvous import (
    _await_exit_or_writer_failure as _real_rendezvous,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum._subprocess_rendezvous import _StdinWriterLog


class CallbackError(RuntimeError):
    """A stream task failure, standing in for a raising output callback."""


class ExitProbe:
    """An exit wait that settles on demand and records its cancellation.

    A probe released before the race models an exit that has already settled; a
    probe left unreleased models a child still running. Recording the
    cancellation is what proves the race took the termination route, because
    cancelling the real exit wait is what terminates and reaps the child.

    ``cancel_waits`` models the other half of that termination: a writer whose
    failure surfaces only while the child is being torn down. The probe settles
    it before its own cancellation completes, so a test can be exact about what
    the race's second read finds rather than relying on loop scheduling.
    """

    def __init__(
        self,
        *,
        exit_code: int = 0,
        ticks: int | None = None,
        cancel_waits: asyncio.Event | None = None,
    ) -> None:
        """Set up the probe's events and the exit it will report."""
        self.cancelled = asyncio.Event()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.exit_code = exit_code
        self.exited_at = 1.5
        self._ticks = ticks
        self._cancel_waits = cancel_waits

    async def wait(self) -> tuple[int, float]:
        """Settle after ``ticks`` loop steps, when released, or by cancellation."""
        self.started.set()
        try:
            if self._ticks is None:
                await self.release.wait()
            else:
                for _ in range(self._ticks):
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled.set()
            if self._cancel_waits is not None:
                await self._cancel_waits.wait()
            raise
        return self.exit_code, self.exited_at


# RUF029: this is the body of a real task, so it must be a coroutine function;
# it fails on its first step, which is what a failed stream looks like.
async def fail_immediately(error: BaseException) -> object:  # ruff: ignore[unused-async]
    """Raise *error* on the task's first step, standing in for a failed stream."""
    raise error


def failed_task(error: BaseException) -> asyncio.Task[object]:
    """Return a task that is already scheduled to fail with *error*."""
    return asyncio.create_task(fail_immediately(error))


async def retrieve(*tasks: asyncio.Task[object]) -> None:
    """Retrieve every task's outcome so no exception is reported as lost."""
    await asyncio.gather(*tasks, return_exceptions=True)


def record_rendezvous(monkeypatch: pytest.MonkeyPatch, seam: str) -> asyncio.Event:
    """Patch the exit rendezvous at *seam*, reporting when it hands the run on.

    A failure that must arrive *after* the exit settled cannot be scheduled by
    racing the rendezvous: the event loop may run the reader first, which would
    quietly exercise the other route and leave this one unproven. Wrapping the
    seam and setting an event once the real rendezvous returns marks the
    earliest moment the caller's teardown owns the task, so a test waits for
    that moment instead of guessing at scheduling.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        The test's patcher, which owns the reversal.
    seam : str
        Dotted name of the module-level name the consumer resolves, which is
        the seam each module documents for tests.

    Returns
    -------
    asyncio.Event
        Set once the real rendezvous behind *seam* has returned.
    """
    returned = asyncio.Event()

    async def record(
        exit_wait: cabc.Coroutine[typ.Any, typ.Any, tuple[int, float]],
        stdin_task: asyncio.Task[object] | None,
        *,
        consumers: cabc.Sequence[asyncio.Task[object] | None] = (),
        stdin_log: _StdinWriterLog | None = None,
    ) -> tuple[int, float]:
        """Delegate to the real rendezvous, then report that it returned."""
        result = await _real_rendezvous(
            exit_wait,
            stdin_task,
            consumers=consumers,
            stdin_log=stdin_log,
        )
        returned.set()
        return result

    monkeypatch.setattr(seam, record)
    return returned
