"""Acquisition and drain failures around the native pump hand-off.

The hand-off acquires its worker descriptors before the buffered drain. These
tests pin the two failure paths that ordering creates. A failed acquisition
resumes the reader and falls back to the Python pump with the buffered prefix
intact. A drain that fails or is cancelled closes the unactivated pair,
resumes the reader, and re-raises the original exception, so that hop settles
without a fallback.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    ScopeConfig,
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
    scoped,
    sh,
)
from cuprum._testing import set_rust_availability_for_testing
from cuprum.adapters.pump_metrics import (
    RUST_PUMP_HANDOFF_TOTAL,
    RUST_PUMP_HELD_PAIRS,
    PumpMetricsHook,
)
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._pump_stream_dispatch_support import (
    _FdTransport,
    _nonblocking_pipe_pair,
    _RecordingWriter,
    clear_backend_caches,
)
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector, owned_fds
from tests.helpers.catalogue import python_catalogue

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")

_PREFIX = b"buffered prefix"


def test_acquisition_failure_skips_drain_and_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed acquisition resumes the reader before the Python pump runs."""
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)
    drain_calls = 0

    async def record_drain(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Record an unexpected buffered drain."""
        del reader, writer
        nonlocal drain_calls
        drain_calls += 1
        await asyncio.sleep(0)

    monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", record_drain)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        lambda **_kwargs: None,
    )

    async def exercise() -> tuple[int, int, int, bytes, bool]:
        """Drive dispatch with a reader whose acquisition must fail."""
        reader = asyncio.StreamReader()
        reader_transport = _FdTransport(read_fd)
        reader.set_transport(typ.cast("asyncio.BaseTransport", reader_transport))
        reader.feed_data(_PREFIX)
        reader.feed_eof()
        writer = _RecordingWriter(write_fd)
        await _pipeline_streams._pump_stream_dispatch(
            reader,
            typ.cast("asyncio.StreamWriter", writer),
        )
        return (
            reader_transport.pause_calls,
            reader_transport.resume_calls,
            drain_calls,
            bytes(writer.written),
            writer.eof,
        )

    with _nonblocking_pipe_pair() as (
        read_fd,
        read_write_fd,
        write_read_fd,
        write_fd,
    ):
        del read_write_fd, write_read_fd
        pauses, resumes, drains, delivered, eof = asyncio.run(exercise())

    assert pauses == 1, "the reader must be paused"
    assert resumes == 1, (
        "a failed acquisition must resume the reader before the fallback"
    )
    assert drains == 0, "a failed acquisition must not drain the buffer"
    assert delivered == _PREFIX, (
        "the Python pump must deliver the still-buffered prefix"
    )
    assert eof, "the Python pump must signal downstream EOF"


class _DrainFailureHarness:
    """Drive a drain failure over an acquired pair and record its release."""

    def __init__(
        self,
        error: BaseException,
        *,
        shutdown_signal: BaseException | None = None,
    ) -> None:
        """Capture the failure and the seams that observe cleanup."""
        self.error = error
        self.shutdown_signal = shutdown_signal
        self.resume_calls = 0
        self.acquired: list[int] = []
        self.events: list[PumpEvent] = []
        self.collector = RecordingCollector()
        self.held_during_drain: float | None = None
        self.close_worker_fd = mock.Mock(
            wraps=_pipeline_stream_native_cleanup._close_native_pump_worker_fd
        )
        self._original_open = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the pause, drain-failure, and recording seams."""
        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            self.pause_reader,
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", self.fail_drain)
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.record_open,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_close_native_pump_worker_fd",
            self.close_worker_fd,
        )

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a pause whose resume records restoration of the reader."""
        del reader
        return _pipeline_stream_fds._ReaderPause(resume=self.resume)

    def resume(self) -> None:
        """Record restoration of the paused reader."""
        self.resume_calls += 1

    def record_open(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the acquisition actually opened."""
        worker_fds = self._original_open(**kwargs)
        if worker_fds is not None:
            self.acquired.extend([worker_fds.reader_fd, worker_fds.writer_fd])
        return worker_fds

    async def fail_drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Raise the parametrized drain failure."""
        del reader, writer
        await asyncio.sleep(0)
        self.held_during_drain = self._latest_held_pairs()
        raise self.error

    def _latest_held_pairs(self) -> float | None:
        """Return the held-pair gauge as the drain sees it."""
        values = self.collector.gauge_values(RUST_PUMP_HELD_PAIRS)
        return values[-1] if values else None

    def raise_shutdown_signal(self, event: PumpEvent) -> None:
        """Raise the configured shutdown signal from the drain outcome."""
        signal = self.shutdown_signal
        if signal is not None and event.phase == "handoff":
            raise signal

    def run(self) -> None:
        """Run the hand-off over a private pipe and assert its rollback."""
        observed: list[BaseException] = []

        async def exercise(
            handoff: _pipeline_stream_native_cleanup._RustPumpHandoff,
        ) -> None:
            """Record the exception the hand-off raises inside the loop."""
            try:
                await _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    handoff=handoff,
                )
            except BaseException as exc:
                observed.append(exc)
                raise

        with (
            owned_fds() as (reader_fd, writer_fd),
            observe_pump(self.events.append),
            observe_pump(PumpMetricsHook(self.collector)),
            observe_pump(self.raise_shutdown_signal),
        ):
            handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            )
            expected = self.shutdown_signal or self.error
            with pytest.raises(type(expected)):
                asyncio.run(exercise(handoff))
            # ``asyncio.run`` rebuilds a cancelled task's exception, so identity
            # is asserted where the hand-off actually raises it.
            assert len(observed) == 1, "the drain failure must be observed exactly once"
            assert observed[0] is expected, (
                "the drain failure must propagate unchanged inside the loop"
            )
            if self.shutdown_signal is not None:
                assert observed[0].__context__ is self.error, (
                    "the drain error must stay reachable as the raised context"
                )
            self._assert_released(reader_fd, writer_fd)
        assert self.resume_calls == 1, "the failure must resume the paused reader once"

    def _assert_released(self, reader_fd: int, writer_fd: int) -> None:
        """Assert the unactivated pair closed once and the transports survived."""
        assert len(self.acquired) == 2, "acquisition must open both worker descriptors"
        assert self.close_worker_fd.call_args_list == [
            mock.call(self.acquired[0]),
            mock.call(self.acquired[1]),
        ], "the unactivated pair must be closed exactly once in reader-first order"
        for fd in self.acquired:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        os.fstat(reader_fd)
        os.fstat(writer_fd)
        assert self.held_during_drain == pytest.approx(1.0), (
            "the acquired pair must be reported as held while the drain runs"
        )
        assert self.collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
            "the hold must be released exactly once, after the pair is closed"
        )
        assert [event.outcome for event in self.events if event.phase == "handoff"] == [
            RustPumpHandoffOutcome.DRAIN_FAILED
        ], "the drain failure must settle the hop with its own bounded outcome"
        assert self.collector.counters == [
            (
                RUST_PUMP_HANDOFF_TOTAL,
                1.0,
                {"outcome": RustPumpHandoffOutcome.DRAIN_FAILED},
            )
        ], "the drain failure must increment one bounded hand-off metric"


@pytest.mark.parametrize(
    "error",
    [OSError("drain failed"), asyncio.CancelledError()],
    ids=["oserror", "cancelled"],
)
def test_drain_failure_closes_acquired_pair_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    """A drain failure releases the acquired pair and re-raises unchanged."""
    harness = _DrainFailureHarness(error)
    harness.install(monkeypatch)

    harness.run()


def test_drain_failure_observer_shutdown_signal_takes_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown signal from a pump observer outranks the drain error."""
    harness = _DrainFailureHarness(
        OSError("drain failed"),
        shutdown_signal=KeyboardInterrupt(),
    )
    harness.install(monkeypatch)

    harness.run()


def test_blocking_setup_failure_releases_the_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused activation releases the held pair and keeps the fallback."""
    collector = RecordingCollector()
    events: list[PumpEvent] = []

    def refuse_blocking(**_kwargs: int) -> typ.NoReturn:
        """Refuse the blocking switch the real activation must roll back."""
        msg = "blocking mode refused"
        raise OSError(msg)

    monkeypatch.setattr(
        _pipeline_stream_fds,
        "_set_stream_fds_blocking",
        refuse_blocking,
    )

    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        with observe_pump(events.append), observe_pump(PumpMetricsHook(collector)):
            handled = asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    handoff=handoff,
                )
            )

    assert handled is False, "a refused activation must keep the Python fallback"
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "the activation exit must release the hold exactly once"
    )
    assert [event.outcome for event in events if event.phase == "handoff"] == [
        RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED
    ], "the activation refusal must report its own bounded outcome"


def test_submission_releases_the_hold_before_native_work_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hold ends when activation hands the pair to the completion callback."""
    collector = RecordingCollector()
    held_at_submission: list[float | None] = []

    def record_submission(
        *,
        state: _pipeline_stream_native_cleanup._RustPumpState,
    ) -> None:
        """Record the hold and release the pair the callback would own."""
        held_at_submission.append(
            (collector.gauge_values(RUST_PUMP_HELD_PAIRS) or [None])[-1]
        )
        _pipeline_stream_native_cleanup._close_native_pump_worker_fd(state.reader_fd)
        _pipeline_stream_native_cleanup._close_native_pump_worker_fd(state.writer_fd)

    # The seam is awaited, so the synchronous double is wrapped rather than
    # given a dummy await just to satisfy the coroutine contract.
    monkeypatch.setattr(
        _pipeline_streams,
        "_run_rust_pump_with_blocking_fds",
        mock.AsyncMock(side_effect=record_submission),
    )

    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        with observe_pump(PumpMetricsHook(collector)):
            handled = asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    handoff=handoff,
                )
            )

    assert handled is True, "an activation that succeeds must report the hand-off"
    assert held_at_submission == [0.0], (
        "the coordinator must hold nothing once the worker owns the pair"
    )
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "the submission exit must release the hold exactly once"
    )


class _HoldSignalHarness:
    """Drive a shutdown signal raised by a hold observer through the hand-off."""

    def __init__(self, *, delta: int) -> None:
        """Capture the hold transition whose observer raises the signal."""
        self.delta = delta
        self.resume_calls = 0
        self.submissions = 0
        self.restores = 0
        self.acquired: list[int] = []
        self.events: list[PumpEvent] = []
        self.collector = RecordingCollector()
        self._original_open = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )
        self._original_restore = (
            _pipeline_stream_native_cleanup._restore_rust_pump_state
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the pause, drain, submission, and rollback seams."""
        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            self.pause_reader,
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", self.drain)
        monkeypatch.setattr(
            _pipeline_streams,
            "_run_rust_pump_with_blocking_fds",
            self.record_submission,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.record_open,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_restore_rust_pump_state",
            self.record_restore,
        )

    def pause_reader(
        self,
        reader: asyncio.StreamReader,
    ) -> _pipeline_stream_fds._ReaderPause:
        """Return a pause whose resume records restoration of the reader."""
        del reader
        return _pipeline_stream_fds._ReaderPause(resume=self.resume)

    def resume(self) -> None:
        """Record restoration of the paused reader."""
        self.resume_calls += 1

    async def drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Complete the buffered drain so the hand-off reaches activation."""
        del reader, writer
        await asyncio.sleep(0)

    async def record_submission(
        self,
        *,
        state: _pipeline_stream_native_cleanup._RustPumpState,
    ) -> None:
        """Record a submission that a shutdown signal must prevent."""
        del state
        self.submissions += 1

    def record_open(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the acquisition actually opened."""
        worker_fds = self._original_open(**kwargs)
        if worker_fds is not None:
            self.acquired.extend([worker_fds.reader_fd, worker_fds.writer_fd])
        return worker_fds

    def record_restore(
        self,
        state: _pipeline_stream_native_cleanup._RustPumpState,
    ) -> None:
        """Record the blocking-mode restoration a rollback performs."""
        self.restores += 1
        self._original_restore(state)

    def raise_signal(self, event: PumpEvent) -> None:
        """Raise the shutdown signal on the configured hold transition."""
        if event.phase == "handoff_hold" and event.held_delta == self.delta:
            raise KeyboardInterrupt

    def run(self) -> None:
        """Run the hand-off and assert the signal left nothing stranded."""
        with owned_fds() as (reader_fd, writer_fd):
            handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            )
            with (
                observe_pump(self.raise_signal),
                observe_pump(PumpMetricsHook(self.collector)),
                observe_pump(self.events.append),
                pytest.raises(KeyboardInterrupt),
            ):
                asyncio.run(
                    _pipeline_streams._run_rust_pump(
                        reader=typ.cast("asyncio.StreamReader", object()),
                        writer=None,
                        handoff=handoff,
                    )
                )
            os.fstat(reader_fd)
            os.fstat(writer_fd)

        assert self.resume_calls == 1, "the signal must resume the paused reader once"
        assert self.submissions == 0, "the signal must prevent the submission"
        assert len(self.acquired) == 2, "acquisition must open both worker descriptors"
        for fd in self.acquired:
            with pytest.raises(OSError, match="Bad file descriptor"):
                os.fstat(fd)
        assert self.collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
            "the release must still reach the metrics hook registered after the "
            "observer that raised"
        )


def test_a_shutdown_signal_at_acquisition_releases_the_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal raised on the hold release closes the unactivated pair."""
    harness = _HoldSignalHarness(delta=1)
    harness.install(monkeypatch)

    harness.run()

    assert harness.restores == 0, "no blocking mode was engaged to restore"
    assert [event.outcome for event in harness.events if event.phase == "handoff"] == [
        RustPumpHandoffOutcome.DRAIN_FAILED
    ], "the interrupted drain window must settle with its own bounded outcome"


def test_a_shutdown_signal_at_release_rolls_back_the_activated_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal raised after activation restores the pair before it travels."""
    harness = _HoldSignalHarness(delta=-1)
    harness.install(monkeypatch)

    harness.run()

    assert harness.restores == 1, (
        "the activated pair must have its blocking mode restored before the "
        "signal travels"
    )
    assert [event.outcome for event in harness.events if event.phase == "handoff"] == [
        RustPumpHandoffOutcome.SUBMISSION_ABANDONED
    ], "an abandoned submission must settle with its own bounded outcome"


def test_an_unexpected_activation_failure_resumes_and_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An activation error outside the decline contract still cleans up."""
    collector = RecordingCollector()
    resume_calls = 0
    acquired: list[int] = []
    original_open = _pipeline_stream_native_cleanup._open_native_pump_worker_fds

    def record_open(
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the acquisition actually opened."""
        worker_fds = original_open(**kwargs)
        if worker_fds is not None:
            acquired.extend([worker_fds.reader_fd, worker_fds.writer_fd])
        return worker_fds

    def resume() -> None:
        """Record restoration of the paused reader."""
        nonlocal resume_calls
        resume_calls += 1

    def fail_activation(
        worker_fds: _pipeline_stream_fds._NativePumpWorkerFds,
        reader_pause: _pipeline_stream_fds._ReaderPause,
        handoff: _pipeline_stream_native_cleanup._RustPumpHandoff,
    ) -> typ.NoReturn:
        """Close the pair the way activation does, then fail unexpectedly."""
        del reader_pause, handoff
        _pipeline_stream_native_cleanup._close_unactivated_rust_pump_fds(worker_fds)
        msg = "activation failed unexpectedly"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(resume=resume),
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_drain_reader_buffer",
        mock.AsyncMock(),
    )
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        record_open,
    )
    monkeypatch.setattr(
        _pipeline_streams,
        "_activate_rust_pump_state",
        fail_activation,
    )

    with owned_fds() as (reader_fd, writer_fd):
        handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            cleanup_grace_s=0.5,
        )
        with (
            observe_pump(PumpMetricsHook(collector)),
            pytest.raises(RuntimeError, match="activation failed unexpectedly"),
        ):
            asyncio.run(
                _pipeline_streams._run_rust_pump(
                    reader=typ.cast("asyncio.StreamReader", object()),
                    writer=None,
                    handoff=handoff,
                )
            )

    assert resume_calls == 1, "an unexpected activation failure must resume the reader"
    assert len(acquired) == 2, "acquisition must open both worker descriptors"
    for fd in acquired:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)
    assert collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "an unexpected activation failure must still release the hold"
    )


def test_acquisition_failure_keeps_public_pipeline_output_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed acquisition falls back without disturbing the public pipeline.

    The hop is forced onto the native backend and its acquisition seam is made
    to decline, so the end-to-end run proves the Python fallback carries the
    payload and both stages complete rather than that the private seam merely
    returns ``False``.
    """
    monkeypatch.setenv("CUPRUM_STREAM_BACKEND", "rust")
    set_rust_availability_for_testing(is_available=True)
    open_worker_fds = mock.Mock(return_value=None)
    monkeypatch.setattr(
        _pipeline_stream_native_cleanup,
        "_open_native_pump_worker_fds",
        open_worker_fds,
    )
    catalogue, python_program = python_catalogue()
    python = sh.make(python_program, catalogue=catalogue)
    producer = python("-c", "import sys; sys.stdout.write('buffered prefix')")
    consumer = python(
        "-c",
        "import sys; sys.stdout.write(sys.stdin.read().upper())",
    )

    with scoped(ScopeConfig(allowlist=frozenset([python_program]))):
        result = (producer | consumer).run_sync(timeout=5.0)

    assert open_worker_fds.call_count == 1, (
        "the hop must reach the acquisition seam, or this test proves nothing"
    )
    assert open_worker_fds.call_args_list[0].kwargs, (
        "acquisition must be asked for the hand-off's descriptors"
    )
    assert result.stdout == "BUFFERED PREFIX", (
        "the Python fallback must deliver the pipeline payload unchanged"
    )
    assert [stage.exit_code for stage in result.stages] == [0, 0], (
        "both stages must complete despite the failed acquisition"
    )
