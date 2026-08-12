"""Pipeline timeout boundary and post-timeout teardown.

Kept apart from ``test_pipeline`` so that module stays about composition and
streaming: these exercise a different concern — what a caller sees when a
deadline expires, and what survives when the caller cancels mid-teardown.

The public-boundary cases have single-command counterparts in
``test_safe_cmd_run``; the property-level invariants live in
``test_subprocess_timeout_properties``.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import contextlib
import os
import signal
import typing as typ
from unittest import mock

import pytest

from cuprum import ScopeConfig, TimeoutExpired, _pipeline_collect, scoped, sh
from cuprum._process_lifecycle import (
    _terminate_pipeline_remaining_stages,
    _terminate_timed_out_stages,
)
from cuprum.sh import ExecutionContext, Pipeline, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.execution import _RunKwargs
from tests.helpers.timeouts import (
    child_argv,
    pending_tasks,
    started_pids,
    stubborn_child_argv,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from cuprum.events import ExecEvent


_PIPELINE_STAGES = 2


# -- Public-boundary timeout contract -----------------------------------------
#
# The single-command counterparts live in ``test_safe_cmd_run``. A pipeline
# enforces one deadline for the whole run rather than one per stage, so these
# additionally pin that every stage is reaped, not just the one that timed out.


type PipelineTimeoutRunFn = cabc.Callable[
    [Pipeline, _RunKwargs],
    tuple[TimeoutExpired, set[asyncio.Task[object]]],
]


def _pipeline_timeout_async(
    pipeline: Pipeline, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run()``, returning the timeout and any tasks left pending."""

    async def run_case() -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
        """Await the run inside a loop that is still open for inspection."""
        with pytest.raises(TimeoutExpired) as exc_info:
            await pipeline.run(**kwargs)
        return exc_info.value, pending_tasks()

    return asyncio.run(run_case())


def _pipeline_timeout_sync(
    pipeline: Pipeline, kwargs: _RunKwargs
) -> tuple[TimeoutExpired, set[asyncio.Task[object]]]:
    """Run via ``run_sync()``; its loop is closed before control returns."""
    with pytest.raises(TimeoutExpired) as exc_info:
        pipeline.run_sync(**kwargs)
    return exc_info.value, set()


@pytest.fixture(params=["async", "sync"], ids=["run()", "run_sync()"])
def pipeline_timeout_strategy(
    request: pytest.FixtureRequest,
) -> PipelineTimeoutRunFn:
    """Provide Pipeline run()/run_sync() strategies that expect a timeout."""
    if request.param == "async":
        return _pipeline_timeout_async
    return _pipeline_timeout_sync


@pytest.mark.parametrize("configured_timeout", [0, -1.0])
@pytest.mark.parametrize("capture", [True, False])
def test_pipeline_non_positive_timeout_at_public_boundary(
    configured_timeout: float,
    pipeline_timeout_strategy: PipelineTimeoutRunFn,
    tmp_path: Path,
    *,
    capture: bool,
) -> None:
    """A non-positive pipeline timeout expires immediately and reaps every stage.

    The deadline is already elapsed, so nothing here waits on wall-clock time:
    the first stage blocks forever and only the timeout can end the run. Both
    stages must be gone afterwards, and no consumer or pipe task may survive.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    pipeline = python(*child_argv(tmp_path / "ready")) | python(
        "-c", "import sys; sys.stdin.read()"
    )
    events: list[ExecEvent] = []

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
    ):
        expired, leaked = pipeline_timeout_strategy(
            pipeline,
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
    assert len(pids) == _PIPELINE_STAGES, (
        f"expected {_PIPELINE_STAGES} spawned stages, got {pids!r}"
    )
    for pid in pids:
        wait_for_process_death(pid, context="the pipeline timeout")

    detail = f"output={expired.output!r} stderr={expired.stderr!r}"
    if capture:
        assert isinstance(expired.output, str), (
            f"a capturing pipeline must surface partial stdout as a string, {detail}"
        )
        assert isinstance(expired.stderr, str), (
            f"a capturing pipeline must surface partial stderr as a string, {detail}"
        )
    else:
        assert expired.output is None, (
            f"a non-capturing pipeline must leave stdout unset, got {detail}"
        )
        assert expired.stderr is None, (
            f"a non-capturing pipeline must leave stderr unset, got {detail}"
        )


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


def test_timeout_teardown_completes_despite_caller_cancellation() -> None:
    """Cancelling mid-grace must not strand a stage before the SIGKILL escalation.

    ``terminate()`` is synchronous and always lands, but the escalation waits
    out ``cancel_grace`` first. If the caller's cancellation interrupted that
    wait, a stage ignoring ``SIGTERM`` would survive the run that spawned it.
    Teardown is therefore shielded and finishes before the cancellation
    propagates.
    """

    async def run_case() -> None:
        """Cancel the teardown inside its grace window and inspect the stage."""
        process = _SigtermImmuneProcess()
        task = asyncio.create_task(
            _terminate_timed_out_stages(
                [typ.cast("asyncio.subprocess.Process", process)], 0.2
            )
        )
        await asyncio.sleep(0.02)
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
                timeout=0.05, output=RunOutputOptions(capture=False), context=ctx
            )
        )
        # ASYNC110: the readiness signal is a file written by another process,
        # which no asyncio.Event can observe. Polling it is the coordination,
        # and it is bounded by the child writing the marker at start-up.
        while not marker.exists():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(events.append),
    ):
        asyncio.run(run_case())

    pids = started_pids(events)
    assert pids, "the pipeline must report at least the first stage's pid"
    for pid in pids:
        wait_for_process_death(pid, context="cancellation during timeout teardown")


# -- Cancellation during fail-fast teardown -----------------------------------


def test_fail_fast_teardown_completes_despite_caller_cancellation() -> None:
    """Fail-fast teardown is shielded like the timeout path.

    ``_terminate_pipeline_remaining_stages`` tears down the surviving stages
    after one exits non-zero. It runs inside the pipeline wait loop, so a
    caller cancelling the run can land on its grace-period wait; without the
    shield the ``SIGKILL`` escalation would be skipped and a ``SIGTERM``-immune
    stage would outlive the pipeline.
    """

    async def run_case() -> None:
        """Cancel fail-fast teardown mid-grace and inspect the surviving stage."""
        failed = _SigtermImmuneProcess()
        failed.returncode = 1
        survivor = _SigtermImmuneProcess()
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
        await asyncio.sleep(0.02)
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


# -- Inter-stage pump ownership on the immediate path -------------------------


def test_zero_timeout_reconciles_pipe_tasks() -> None:
    """A zero deadline must settle the inter-stage pumps the caller owns.

    The pumps exist before ``asyncio.wait_for`` is entered, and a zero timeout
    cancels ``_wait_for_pipeline`` before its ``finally`` can reconcile them.
    Terminating the stages settles them as a side effect, masking whether
    anything owns them, so termination is stubbed out here: the first stage
    writes more than a pipe buffer holds and the second never reads, leaving
    the pump genuinely blocked when the deadline fires.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    created: list[asyncio.Task[None]] = []
    events: list[ExecEvent] = []
    real_create = _pipeline_collect._create_pipe_tasks

    def spy(processes: list[asyncio.subprocess.Process]) -> list[asyncio.Task[None]]:
        """Record the pumps the pipeline creates so they can be inspected."""
        tasks = real_create(processes)
        created.extend(tasks)
        return tasks

    async def no_termination(
        processes: cabc.Iterable[asyncio.subprocess.Process],
        cancel_grace: float,
    ) -> None:
        """Stand in for stage termination without settling anything."""
        _ = (processes, cancel_grace)
        await asyncio.sleep(0)

    pipeline = python(
        "-c", "import sys, time; sys.stdout.write('x' * 10_000_000); time.sleep(30)"
    ) | python("-c", "import time; time.sleep(30)")

    async def run_case() -> None:
        """Time out immediately, then inspect the pumps before the loop closes.

        ``asyncio.run`` cancels everything still pending during shutdown, so a
        stranded pump looks settled from outside. The assertion has to happen
        while the loop is still live.
        """
        with pytest.raises(TimeoutExpired):
            await pipeline.run(timeout=0, output=RunOutputOptions(capture=False))

        assert created, "the pipeline must create an inter-stage pump to reconcile"
        for index, task in enumerate(created):
            assert task.done(), (
                f"pump {index} was left unsettled after the immediate timeout: "
                "nothing reconciled the pumps the caller owns"
            )

    try:
        with (
            mock.patch.object(_pipeline_collect, "_create_pipe_tasks", spy),
            mock.patch.object(
                _pipeline_collect, "_terminate_timed_out_stages", no_termination
            ),
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(events.append),
        ):
            asyncio.run(run_case())
    finally:
        for pid in started_pids(events):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
