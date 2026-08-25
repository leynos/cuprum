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
from cuprum._pipeline_stream_results import _reconcile_pipe_tasks
from cuprum._process_lifecycle import (
    _shielded_cleanup,
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
    from pathlib import Path

    from cuprum.events import ExecEvent


_PIPELINE_STAGES = 2

# Long enough that the immune child reaches ``signal.signal`` before the
# deadline reaps it, and short enough to keep the test quick.
_STAGE_TIMEOUT = 1.0
_CANCEL_GRACE = 1.0


async def _poll_until(
    predicate: cabc.Callable[[], bool],
    message: str,
    *,
    seconds: float = 10.0,
) -> None:
    """Yield until ``predicate`` holds, failing rather than hanging.

    Parameters
    ----------
    predicate:
        Readiness check, polled until it returns true.
    message:
        Failure message reported if ``seconds`` elapses first.
    seconds:
        How long to wait before failing.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    # ASYNC110: the readiness signal is a file written by another process,
    # which no asyncio.Event can observe. Polling it is the coordination.
    while not predicate():
        if loop.time() > deadline:
            pytest.fail(message)
        await asyncio.sleep(0.01)


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
        self.terminate_signalled = asyncio.Event()
        self.grace_started = asyncio.Event()
        self._exited = asyncio.Event()

    async def wait(self) -> int:
        """Block until ``kill()`` records an exit code.

        Entry is announced so a test can place a cancellation *inside* the
        grace-period wait: ``terminate()`` has already returned by this point,
        so the escalation to ``SIGKILL`` is the only thing still outstanding.

        Returns
        -------
        int
            The recorded exit code, or ``0`` when none was set.
        """
        self.grace_started.set()
        await self._exited.wait()
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        """Record the signal without exiting, as a SIGTERM-immune child would."""
        self.terminated = True
        self.terminate_signalled.set()

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


def test_timeout_teardown_survives_repeated_cancellation() -> None:
    """A second cancellation during the grace period must not strand a stage.

    Cancelling a task propagates to whatever future it is awaiting, so a
    further ``cancel()`` landing while teardown is being waited out would
    cancel the teardown itself and skip the ``SIGKILL`` escalation. The wait is
    therefore retried under a shield until it completes, however many
    cancellations arrive.
    """

    async def run_case() -> None:
        """Cancel twice inside the grace window and inspect the stage."""
        process = _SigtermImmuneProcess()
        task = asyncio.create_task(
            _terminate_timed_out_stages(
                [typ.cast("asyncio.subprocess.Process", process)], 0.2
            )
        )
        # Block until teardown is actually waiting out the grace period, so
        # both cancellations land inside it rather than before the initial
        # signal was even delivered.
        await process.grace_started.wait()

        assert task.cancel(), "the first cancellation must reach a live teardown"
        # One scheduler pass delivers that cancellation; the retry loop then
        # re-parks on the shield within the same pass. The count is not what
        # the test rests on — the assertion below is: `cancel()` returns False
        # once a task has finished, so a second cancellation that is accepted
        # proves the first did not end the teardown early.
        await asyncio.sleep(0)
        assert task.cancel(), (
            "the teardown completed on the first cancellation, so the retry "
            "path this test exists to cover was never exercised"
        )

        with pytest.raises(asyncio.CancelledError):
            await task

        assert process.killed, (
            "the SIGKILL escalation must complete even when a second "
            "cancellation lands during the grace-period wait"
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
        ctx = ExecutionContext(cancel_grace=_CANCEL_GRACE)
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        task = asyncio.create_task(
            pipeline.run(
                timeout=_STAGE_TIMEOUT,
                output=RunOutputOptions(capture=False),
                context=ctx,
            )
        )
        # The deadline has to outlast interpreter start-up. A ``SIGTERM``
        # arriving before the child reaches ``signal.signal`` kills it outright,
        # so it never writes the marker and never becomes the immune stage this
        # test is about — and the wait below would then never finish.
        await _poll_until(
            marker.exists, "the immune first stage never signalled readiness"
        )
        # Teardown waits out ``cancel_grace`` after the deadline expires, and
        # the cancellation belongs inside that window. There is no event to
        # wait on: the ``timeout`` events are not emitted until teardown has
        # already finished, so this is timed from the deadline itself.
        delay = started_at + _STAGE_TIMEOUT + 0.2 - loop.time()
        assert delay > 0, (
            "the deadline elapsed before the child signalled readiness, so the "
            "cancellation below would land after the grace window instead of "
            f"inside it and quietly stop covering it (delay={delay!r})"
        )
        await asyncio.sleep(delay)
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


def test_collector_holds_its_cancellation_until_reconciliation_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_collect_pipeline_inputs`` must not unwind mid-reconciliation.

    Driven through the collector itself rather than the primitive, with the
    reconciliation gated at its boundary so the cancellation is delivered while
    the ``finally`` is genuinely in flight. Unshielded, the cancellation would
    reach the gated coroutine and the collector would unwind immediately,
    leaving the pumps it owns with nobody to await them.
    """
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    gate = asyncio.Event()
    entered = asyncio.Event()
    reconciled = False

    real_reconcile = _pipeline_collect._reconcile_pipe_tasks

    async def gated_reconcile(pipe_tasks: list[asyncio.Task[None]]) -> None:
        """Announce entry, block until released, then reconcile for real."""
        nonlocal reconciled
        entered.set()
        await gate.wait()
        await real_reconcile(pipe_tasks)
        reconciled = True

    monkeypatch.setattr(_pipeline_collect, "_reconcile_pipe_tasks", gated_reconcile)

    pipeline = python("-c", "print('a')") | python("-c", "import sys; sys.stdin.read()")

    async def run_case() -> None:
        """Cancel the collector while its reconciliation is gated."""
        task = asyncio.create_task(pipeline.run(output=RunOutputOptions(capture=False)))
        await entered.wait()

        assert task.cancel(), "the cancellation must reach a live collector"
        for _ in range(8):
            await asyncio.sleep(0)
        assert not task.done(), (
            "the collector unwound while its reconciliation was still running, "
            "so the pumps it owns would be left with nobody to await them"
        )

        gate.set()
        with contextlib.suppress(asyncio.CancelledError, BaseExceptionGroup):
            await task
        assert reconciled, "the reconciliation must have run to completion"

    with (
        scoped(ScopeConfig(allowlist=frozenset([python_program]))),
        sh.observe(lambda _event: None),
    ):
        asyncio.run(run_case())


def test_pipe_task_reconciliation_survives_repeated_cancellation() -> None:
    """Repeated cancellation must not abandon the inter-stage pumps.

    ``_reconcile_pipe_tasks`` cancels each pump and then gathers them. The
    invariant that matters to the pipeline is the one asserted below: however
    many cancellations arrive, no pump is left running once the cancellation
    propagates, because nothing downstream would await it.

    This holds with or without the shield, because ``gather`` with
    ``return_exceptions`` awaits its children even when the gather itself is
    cancelled. What the shield buys is covered by
    :func:`test_collector_holds_its_cancellation_until_reconciliation_finishes`,
    which gates the reconciliation so the collector is caught mid-``finally``.
    """

    async def run_case() -> None:
        """Cancel twice while the pumps are still settling, then inspect them."""
        released = asyncio.Event()

        async def stubborn_pump() -> None:
            """Absorb cancellation until released, as a pump mid-write would."""
            while not released.is_set():
                with contextlib.suppress(asyncio.CancelledError):
                    await released.wait()

        pipe_tasks = [asyncio.create_task(stubborn_pump()) for _ in range(2)]
        # One scheduler pass parks each pump, so the reconciliation's cancel
        # lands on a running task rather than one the loop has not started.
        await asyncio.sleep(0)

        task = asyncio.create_task(_shielded_cleanup(_reconcile_pipe_tasks(pipe_tasks)))
        await asyncio.sleep(0)

        for _ in range(2):
            assert task.cancel(), "the cancellation must reach a live reconciliation"
            await asyncio.sleep(0)

        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        for index, pipe_task in enumerate(pipe_tasks):
            assert pipe_task.done(), (
                f"pump {index} was left running after the cancellation propagated"
            )

    asyncio.run(run_case())
