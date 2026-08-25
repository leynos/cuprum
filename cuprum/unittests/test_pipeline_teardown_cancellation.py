"""Caller cancellation arriving during pipeline stage teardown.

Split from ``test_pipeline_timeouts`` so that module stays about what a caller
sees when a deadline expires. These cases are about the other half: what
survives when the caller cancels while teardown is mid-flight. Both teardown
routes — the post-timeout one and the fail-fast one — escalate ``SIGTERM`` to
``SIGKILL`` across a grace period, and a cancellation landing on that grace
wait would skip the escalation and orphan a ``SIGTERM``-immune stage.
"""

from __future__ import annotations

import asyncio
import contextlib
import typing as typ

import pytest

from cuprum import ScopeConfig, TimeoutExpired, scoped, sh
from cuprum._process_lifecycle import (
    _terminate_pipeline_remaining_stages,
    _terminate_timed_out_stages,
)
from cuprum.sh import ExecutionContext, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.timeouts import (
    started_pids,
    stubborn_child_argv,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

    from cuprum.events import ExecEvent

# Long enough that two cancellations are delivered well inside the grace
# window; the escalation, not the clock, is what ends the teardown.
_LONG_GRACE = 0.5
# Event-loop turns to yield so a cancellation reaches the target task.
_CANCELLATION_DELIVERY_TURNS = 3


# -- Cancellation during post-timeout teardown --------------------------------


class _SigtermImmuneProcess:
    """Process double that ignores ``terminate()``; only ``kill()`` ends it."""

    def __init__(self) -> None:
        """Start unexited, recording which signals were delivered."""
        self.returncode: int | None = None
        self.pid = 9101
        self.terminated = False
        self.killed = False
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Block until ``kill()`` records an exit code."""
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        """Record the signal without exiting, as a SIGTERM-immune child would."""
        self.terminated = True

    def kill(self) -> None:
        """Record the escalation and exit."""
        self.killed = True
        self.returncode = -9
        self._exited.set()


class _SignallingImmuneProcess(_SigtermImmuneProcess):
    """SIGTERM-immune double that announces when ``terminate()`` lands."""

    def __init__(self) -> None:
        """Start unexited, with the signal announcement unset."""
        super().__init__()
        self.signalled = asyncio.Event()

    def terminate(self) -> None:
        """Record the signal and announce that the grace window has opened."""
        super().terminate()
        self.signalled.set()


def test_timeout_teardown_completes_despite_caller_cancellation() -> None:
    """Cancelling mid-grace must not strand a stage before the SIGKILL escalation.

    ``terminate()`` is synchronous and always lands, but the escalation waits
    out ``cancel_grace`` first. If the caller's cancellation interrupted that
    wait, a stage ignoring ``SIGTERM`` would survive the run that spawned it.
    Teardown is therefore shielded and finishes before the cancellation
    propagates. The stage announces the signal, so the cancellation is timed
    against the opening of the grace window rather than against the clock.
    """

    async def run_case() -> None:
        """Cancel the teardown inside its grace window and inspect the stage."""
        process = _SignallingImmuneProcess()
        task = asyncio.create_task(
            _terminate_timed_out_stages(
                [typ.cast("asyncio.subprocess.Process", process)], 0.2
            )
        )
        await process.signalled.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.terminated, "teardown must still deliver the initial signal"
        assert process.killed, (
            "the SIGKILL escalation must complete before the cancellation "
            "propagates, otherwise a SIGTERM-immune stage outlives the run"
        )
        assert process.returncode == -9, (
            f"the stage must be reaped, got returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


async def _deliver_cancellation(task: asyncio.Task[object]) -> None:
    """Cancel ``task`` and yield until the cancellation has been delivered."""
    task.cancel()
    for _ in range(_CANCELLATION_DELIVERY_TURNS):
        await asyncio.sleep(0)


def test_teardown_completes_despite_a_second_caller_cancellation() -> None:
    """A second cancellation must not reach the teardown it is held off from.

    ``asyncio.shield`` protects only the await it wraps, so a teardown that is
    re-awaited bare after the first cancellation is no longer shielded: the
    next cancellation lands on the escalation itself, skipping ``SIGKILL``.
    Both cancellations are delivered inside the grace window — asserted below,
    so the case cannot pass vacuously by racing the escalation.
    """

    async def run_case() -> None:
        """Cancel the waiter twice mid-grace and inspect the stage."""
        process = _SignallingImmuneProcess()
        task = asyncio.create_task(
            _terminate_timed_out_stages(
                [typ.cast("asyncio.subprocess.Process", process)], _LONG_GRACE
            )
        )
        await process.signalled.wait()
        await _deliver_cancellation(task)
        await _deliver_cancellation(task)

        assert not process.killed, (
            "both cancellations must land while the grace period is still "
            "running, otherwise this case proves nothing"
        )

        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.killed, (
            "the SIGKILL escalation must survive a second cancellation; "
            "an unshielded retry lets it cancel the teardown instead"
        )
        assert process.returncode == -9, (
            f"the stage must be reaped, got returncode={process.returncode!r}"
        )

    asyncio.run(run_case())


async def _wait_for_marker(marker: Path, *, seconds: float = 10.0) -> None:
    """Fail unless ``marker`` appears within ``seconds``.

    The asyncio counterpart of ``wait_for_process_death``. The readiness signal
    is a file written by another process, which no ``asyncio.Event`` can
    observe, so polling is the coordination. The deadline keeps a child that
    never reaches its handler installation from hanging the session instead of
    failing it.

    Parameters
    ----------
    marker : Path
        The readiness file the child writes once it is genuinely immune.
    seconds : float
        How long to wait before failing, in seconds.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while loop.time() < deadline:
        # ASYNC240: the readiness signal is a file written by another process,
        # which no asyncio primitive can observe. Stat-ing it is the
        # coordination, and it is cheap enough not to stall the loop.
        if marker.exists():  # noqa: ASYNC240
            return
        await asyncio.sleep(0.01)
    pytest.fail(  # pragma: no cover - defensive failure
        f"the child never announced its SIGTERM handler within {seconds}s",
    )


def test_pipeline_run_cancelled_during_grace_still_reaps_stages(
    tmp_path: Path,
) -> None:
    """Cancelling ``Pipeline.run()`` mid-teardown still leaves no stage running.

    The end-to-end counterpart of the helper test above: a real first stage
    ignores ``SIGTERM``, so only the escalation can reap it. Cancellation is
    issued once the child has confirmed its handler is installed, so the test
    does not race interpreter start-up.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    marker = tmp_path / "immune"
    pipeline = python(*stubborn_child_argv(marker)) | python(
        "-c", "import sys; sys.stdin.read()"
    )
    events: list[ExecEvent] = []

    async def run_case() -> None:
        """Time out, then cancel while the grace period is still running."""
        ctx = ExecutionContext(cancel_grace=1.0)
        task = asyncio.create_task(
            pipeline.run(
                timeout=2.0, output=RunOutputOptions(capture=False), context=ctx
            )
        )
        await _wait_for_marker(marker)
        # The timeout begins when the pipeline starts, so leave enough room for
        # Python to install and announce the child handler before entering the
        # timeout teardown's grace period.
        await asyncio.sleep(2.15)
        task.cancel()
        # The run is cancelled mid-teardown, so the shielded teardown re-raises
        # CancelledError. Should the cancellation instead arrive after teardown
        # finished, the expired deadline surfaces as TimeoutExpired. Neither is
        # what this case asserts; the reaping check below is.
        with contextlib.suppress(asyncio.CancelledError, TimeoutExpired):
            await task

    try:
        with (
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(events.append),
        ):
            asyncio.run(run_case())

        assert started_pids(events), (
            "the pipeline must report at least the first stage's pid"
        )
    finally:
        for pid in started_pids(events):
            wait_for_process_death(
                pid,
                context="cancellation during timeout teardown",
            )


# -- Cancellation during fail-fast teardown -----------------------------------


def test_fail_fast_teardown_completes_despite_caller_cancellation() -> None:
    """Fail-fast teardown is shielded like the timeout path.

    ``_terminate_pipeline_remaining_stages`` tears down the surviving stages
    after one exits non-zero. It runs inside the pipeline wait loop, so a
    caller cancelling the run can land on its grace-period wait; without the
    shield the ``SIGKILL`` escalation would be skipped and a ``SIGTERM``-immune
    stage would outlive the pipeline. The surviving stage announces the signal,
    so the cancellation is timed against the opening of the grace window rather
    than against the clock.
    """

    async def run_case() -> None:
        """Cancel fail-fast teardown mid-grace and inspect the surviving stage."""
        failed = _SigtermImmuneProcess()
        failed.returncode = 1
        survivor = _SignallingImmuneProcess()
        wait_tasks = [
            asyncio.create_task(_settled(1)),
            asyncio.create_task(survivor.wait()),
        ]
        await asyncio.sleep(0)

        task = asyncio.create_task(
            _terminate_pipeline_remaining_stages(
                [
                    typ.cast("asyncio.subprocess.Process", failed),
                    typ.cast("asyncio.subprocess.Process", survivor),
                ],
                wait_tasks,
                0,
                cancel_grace=0.2,
            )
        )
        await survivor.signalled.wait()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert survivor.killed, (
            "the SIGKILL escalation must complete before the cancellation "
            "propagates, otherwise a SIGTERM-immune stage outlives the pipeline"
        )
        for wait_task in wait_tasks:
            wait_task.cancel()

    asyncio.run(run_case())


async def _settled(code: int) -> int:
    """Yield once, then return ``code``, standing in for an exited stage."""
    await asyncio.sleep(0)
    return code
