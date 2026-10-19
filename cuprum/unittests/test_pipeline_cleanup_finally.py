"""A pipeline's capture sinks are released even when cleanup itself fails.

Every failure path composes its observe-task drain with its capture release:
``_reconcile_pipeline_run_failure``, ``_finalize_timeout_cleanup``, and
``_finalize_result_failure_cleanup``. The drain re-raises a failing observe task
as a ``BaseExceptionGroup``, so a composition that simply awaited the release
next would lose the file exactly on the path where the run is already in
trouble. These tests fail the drain and assert the sink still closed, and that
the group the drain raised is what reached the caller.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import io

import pytest

from cuprum._capture_types import CaptureFinal, CaptureSinkRegistry
from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._pipeline_finalize import (
    _finalize_result_failure_cleanup,
    _finalize_timeout_cleanup,
    _reconcile_pipeline_run_failure,
)
from cuprum._pipeline_records import (
    _PipelineObservers,
    _PipelineSpawnResult,
    _StageWaitContext,
)
from cuprum._sink_lifecycle import _SinkBracket
from cuprum.sh import ExecutionContext


class _ObserveTaskError(Exception):
    """Raised by the observe task these tests fail during cleanup."""


class _SinkWitness:
    """A capture sink that records whether it was cleaned up."""

    def __init__(self) -> None:
        """Start uncleaned."""
        self.cleaned = False

    async def absorb(self, chunk: bytes) -> None:
        """Accept output, which these tests never send."""

    async def finalize(self) -> CaptureFinal:
        """Refuse to finalize; cleanup is the path under test."""
        raise NotImplementedError

    async def cleanup(self) -> None:
        """Record the release."""
        self.cleaned = True


class _Scenario:
    """A sink, a spawn holding it, and observers whose drain will fail."""

    def __init__(self, sink: _SinkWitness) -> None:
        """Build the registry and register *sink* with it."""
        registry = CaptureSinkRegistry()
        registry.register(sink)
        self.sink = sink
        self.spawn = _PipelineSpawnResult(
            processes=[],
            stderr_tasks=[],
            stdout_task=None,
            relay_diagnostics_by_stage=(),
            stages=_StageWaitContext(started_at=()),
            captures=registry,
        )
        self.observers = _PipelineObservers(observations=(), pending_tasks=[])
        # A run that captured nothing and opted into no delivery: this test
        # exercises the teardown composition, and the reconcile path reads
        # exactly these two fields off the config.
        self.config = _PipelineRunConfig(
            ctx=ExecutionContext(),
            capture_stdout=None,
            capture_stderr=None,
            echo_stdout=False,
            echo_stderr=False,
            max_echo_line_bytes=None,
            timeout=None,
            stdout_sink=io.StringIO(),
            stderr_sink=io.StringIO(),
            sink_bracket=_SinkBracket(None),
        )

    def fail_next_drain(self) -> None:
        """Schedule an observe task that raises when cleanup awaits it."""

        async def failing_observe_task() -> None:
            """Fail once the drain awaits it."""
            # Yield first, as a real observe hook would reach its body through
            # the awaited gather; the raise is what the drain then surfaces.
            await asyncio.sleep(0)
            raise _ObserveTaskError

        self.observers.pending_tasks.append(asyncio.create_task(failing_observe_task()))


def _assert_drain_failure_preserved(error_group: BaseExceptionGroup) -> None:
    """Assert the drain failure is grouped with, not replaced by, the release."""
    assert any(
        isinstance(member, _ObserveTaskError) for member in error_group.exceptions
    ), f"the drain failure must survive into the group; got {error_group!r}"


_SinkCleanup = cabc.Callable[
    [_PipelineObservers, _PipelineSpawnResult, BaseException],
    cabc.Awaitable[None],
]


@pytest.mark.parametrize(
    "cleanup",
    [
        pytest.param(_finalize_timeout_cleanup, id="timeout"),
        pytest.param(_finalize_result_failure_cleanup, id="result-failure"),
    ],
)
def test_a_failing_drain_still_releases_the_capture_sink(
    cleanup: _SinkCleanup,
) -> None:
    """The release runs even though the drain raises out of the composition."""

    async def run() -> None:
        """Fail a drain and expect the group while the sink closes."""
        scenario = _Scenario(_SinkWitness())
        scenario.fail_next_drain()

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await cleanup(scenario.observers, scenario.spawn, RuntimeError("active"))

        _assert_drain_failure_preserved(exc_info.value)
        assert scenario.sink.cleaned, (
            "the drain failed, but the spill file must still be released"
        )

    asyncio.run(run())


def test_a_failing_drain_still_releases_the_sink_after_a_run_failure() -> None:
    """The run-failure path releases its sinks after a failing drain too."""

    async def run() -> None:
        """Fail a drain on the run-failure path and expect the group."""
        scenario = _Scenario(_SinkWitness())
        scenario.fail_next_drain()

        with pytest.raises(BaseExceptionGroup) as exc_info:
            await _reconcile_pipeline_run_failure(
                scenario.config,
                scenario.spawn,
                scenario.observers,
                RuntimeError("run failed"),
            )

        _assert_drain_failure_preserved(exc_info.value)
        assert scenario.sink.cleaned, (
            "the drain failed, but the spill file must still be released"
        )

    asyncio.run(run())
