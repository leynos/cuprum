"""A pipeline's capture sinks are released even when cleanup itself fails.

Every failure path composes its observe-task drain with its capture release:
``_reconcile_pipeline_run_failure``, ``_finalize_timeout_cleanup``, and
``_finalize_result_failure_cleanup``. The drain re-raises a failing observe task
as a ``BaseExceptionGroup``, so a composition that simply awaited the release
next would lose the file exactly on the path where the run is already in
trouble. These tests fail the drain and assert the sink still closed, and that
the group the drain raised is what reached the caller.

The drain is not the only step that can end a teardown early. A presentation
session's ``close`` may raise, and a shielded close re-raises the caller's
cancellation once its operation completes. Both happen *before* the drain, so
the release also has to be reached through a ``finally`` that encloses those
steps; the last two cases here pin that for the composed finalizers and for a
cancellation arriving after a real spill file exists.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import io
import threading
import typing as typ

import pytest

from cuprum._capture_decode import _DecodeSettings
from cuprum._capture_file import _FileAccumulator
from cuprum._capture_types import CaptureFinal, CaptureSinkRegistry
from cuprum._pipeline_config import _PipelineRunConfig
from cuprum._pipeline_finalize import (
    _finalize_pipeline_stage_result_failure,
    _finalize_pipeline_timeout,
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
from cuprum.capture_policy import FileBackedCapture
from cuprum.sh import ExecutionContext

if typ.TYPE_CHECKING:
    import pathlib

    from cuprum.sinks import SessionOutcome

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
# A spill threshold of one byte makes the first absorb cross it, so the test
# has a real file on disk rather than a registry entry that names nothing.
_SPILL_THRESHOLD_BYTES = 1
# Long enough that reaching a deadlock is diagnosed as one, short enough that a
# test suite interrupted mid-case still ends on its own.
_BLOCKED_CLOSE_TIMEOUT_S = 30.0


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


class _RaisingSession:
    """A presentation session whose ``close`` always raises."""

    def __init__(self, error: BaseException) -> None:
        """Bind the error ``close`` reports."""
        self.error = error
        self.log = io.StringIO()
        self.closed = False

    def close(self, outcome: SessionOutcome) -> None:
        """Record the attempt, then fail the way a broken adapter would."""
        self.closed = True
        raise self.error


class _BlockingSession:
    """A presentation session whose ``close`` waits to be released.

    Bounded delivery closes the session on a daemon worker, so holding it there
    puts a cancellation in the window *after* the run has decided to stop and
    *before* its capture sinks are released.
    """

    def __init__(
        self,
        started: threading.Event,
        release: threading.Event,
    ) -> None:
        """Bind the thread events that signal entry and grant release."""
        self.started = started
        self.release = release
        self.log = io.StringIO()

    def close(self, outcome: SessionOutcome) -> None:
        """Signal that the close is in flight, then wait for the test."""
        self.started.set()
        if not self.release.wait(_BLOCKED_CLOSE_TIMEOUT_S):
            pytest.fail("the blocked session close was never released")


class _Scenario:
    """A sink, a spawn holding it, and observers whose drain will fail."""

    def __init__(
        self,
        sink: _SinkWitness,
        *,
        sink_bracket: _SinkBracket | None = None,
    ) -> None:
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
            sink_bracket=_SinkBracket(None) if sink_bracket is None else sink_bracket,
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


_Finalizer = cabc.Callable[[_Scenario, BaseException], cabc.Awaitable[None]]


async def _finalize_timeout(scenario: _Scenario, error: BaseException) -> None:
    """Drive the timeout finalizer as the driver calls it."""
    await _finalize_pipeline_timeout(
        scenario.config, scenario.spawn, scenario.observers, error
    )


async def _finalize_stage_result(scenario: _Scenario, error: BaseException) -> None:
    """Drive the stage-result finalizer as the driver calls it."""
    await _finalize_pipeline_stage_result_failure(
        scenario.spawn,
        scenario.observers,
        scenario.config.sink_bracket,
        error,
    )


async def _finalize_run_failure(scenario: _Scenario, error: BaseException) -> None:
    """Drive the run-failure finalizer as the driver calls it."""
    await _reconcile_pipeline_run_failure(
        scenario.config,
        scenario.spawn,
        scenario.observers,
        error,
    )


@pytest.mark.parametrize(
    "finalize",
    [
        pytest.param(_finalize_timeout, id="timeout"),
        pytest.param(_finalize_stage_result, id="stage-result"),
        pytest.param(_finalize_run_failure, id="run-failure"),
    ],
)
def test_a_raising_session_close_still_releases_the_capture_sink(
    finalize: _Finalizer,
) -> None:
    """A presentation sink that fails to close must not strand the spill file.

    The session close precedes the drain on every finalizer, and a close that
    raises ends the teardown there. The capture release therefore has to be
    reached from a ``finally`` that encloses the close; guarding only the drain
    would leave the spill file on disk on the one path where an adapter is
    already misbehaving. The session's own error still reaches the caller.
    """
    close_error = RuntimeError("the presentation sink refused to close")

    async def run() -> None:
        """Close a raising session and expect its error and the release."""
        session = _RaisingSession(close_error)
        scenario = _Scenario(
            _SinkWitness(),
            sink_bracket=_SinkBracket(session),
        )

        with pytest.raises(RuntimeError) as exc_info:
            await finalize(scenario, RuntimeError("the run failed"))

        assert exc_info.value is close_error, (
            f"the session's own failure must reach the caller; got {exc_info.value!r}"
        )
        assert session.closed, "the case must have exercised the session close"
        assert scenario.sink.cleaned, (
            "the session failed to close, but the spill file must still be released"
        )

    asyncio.run(run())


def test_a_cancellation_during_a_bounded_session_close_still_releases_the_sink(
    tmp_path: pathlib.Path,
) -> None:
    """A cancelled timeout teardown removes the spill file it witnessed.

    ``_shielded_cleanup`` completes the close it was given and then re-raises
    the caller's cancellation, so a cancellation that lands while the bounded
    session close is waiting for its worker aborts the rest of the teardown.
    The capture release sits in the enclosing ``finally`` for exactly this
    case: the run has already stopped, and the file its consumers created has
    no result left to name it. The spill file is asserted to exist before the
    cancellation, because an assertion against an empty directory would prove
    nothing about the release.
    """
    payload = b"captured before the cancellation\n"
    started = threading.Event()
    release = threading.Event()
    session = _BlockingSession(started, release)
    bracket = _SinkBracket(session, bounded_delivery=True, delivery_grace_s=None)

    async def run() -> None:
        """Spill, then cancel the timeout finalizer while the close is held."""
        accumulator = _FileAccumulator(
            policy=FileBackedCapture(
                spill_threshold_bytes=_SPILL_THRESHOLD_BYTES,
                directory=tmp_path,
            ),
            stream="stderr",
            settings=_TEXT,
        )
        await accumulator.absorb(payload)
        entries = _entries(tmp_path)
        assert len(entries) == 1, (
            f"the capture had to spill before it could be stranded; found {entries}"
        )

        scenario = _Scenario(_SinkWitness(), sink_bracket=bracket)
        scenario.spawn.captures.register(accumulator)
        finalizing = asyncio.create_task(
            _finalize_pipeline_timeout(
                scenario.config,
                scenario.spawn,
                scenario.observers,
                TimeoutError("the pipeline deadline expired"),
            )
        )
        await _await_thread_event(started)
        finalizing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await finalizing

        assert scenario.sink.cleaned, (
            "the cancellation must not bypass the capture release"
        )
        assert not _entries(tmp_path), (
            "the cancelled teardown must leave no spill file behind"
        )

    asyncio.run(run())


def _entries(directory: pathlib.Path) -> list[pathlib.Path]:
    """List *directory* once, from synchronous code.

    ``pathlib`` reads inside an async function trip the ASYNC240 lint, and a
    test-owned ``tmp_path`` does not warrant an async path library. Reading it
    between awaits, in a helper whose blocking is obvious, is the honest shape.

    Returns
    -------
    list[pathlib.Path]
        The directory's entries, in the order ``iterdir`` yielded them.
    """
    return list(directory.iterdir())


async def _await_thread_event(event: threading.Event) -> None:
    """Fail the case unless *event* is set before the polling deadline."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _BLOCKED_CLOSE_TIMEOUT_S
    while loop.time() < deadline:
        if event.is_set():
            return
        await asyncio.sleep(0.01)
    pytest.fail("the bounded session close never began")
