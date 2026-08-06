"""Pipeline timeout boundary and post-timeout teardown.

Kept apart from ``test_pipeline`` so that module stays about composition and
streaming: these exercise a different concern — what a caller sees when a
deadline expires. Cancellation arriving during the resulting teardown is its
own concern and lives in ``test_pipeline_teardown_cancellation``.

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

from cuprum import ScopeConfig, TimeoutExpired, _pipeline_internals, scoped, sh
from cuprum.sh import Pipeline, RunOutputOptions
from tests.helpers.catalogue import python_catalogue
from tests.helpers.execution import _RunKwargs
from tests.helpers.timeouts import (
    child_argv,
    pending_tasks,
    started_pids,
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
    real_create = _pipeline_internals._create_pipe_tasks

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
            mock.patch.object(_pipeline_internals, "_create_pipe_tasks", spy),
            mock.patch.object(
                _pipeline_internals, "_terminate_timed_out_stages", no_termination
            ),
            scoped(ScopeConfig(allowlist=frozenset([python_program]))),
            sh.observe(events.append),
        ):
            asyncio.run(run_case())
    finally:
        for pid in started_pids(events):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
