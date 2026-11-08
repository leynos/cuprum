"""Hand-off faults at the blocking-mode guard and at its hold release.

The native hand-off settles a hold with an outcome on every failed path. These
tests drive whole hand-offs rather than the seams in isolation, pinning two
properties: a guard that cannot be built leaves the transport's shared flags in
their prior mode, and a hold release that raises a shutdown signal still counts
the outcome the hop settled with.
"""

from __future__ import annotations

import asyncio
import os
import typing as typ
from unittest import mock

import pytest

from cuprum import (
    _pipeline_stream_fds,
    _pipeline_stream_native_cleanup,
    _pipeline_streams,
)
from cuprum.adapters.pump_metrics import RUST_PUMP_HELD_PAIRS, PumpMetricsHook
from cuprum.pump_events import PumpEvent, RustPumpHandoffOutcome
from cuprum.pump_observation import observe_pump
from cuprum.unittests._pump_stream_dispatch_support import clear_backend_caches
from cuprum.unittests._rust_pump_test_helpers import RecordingCollector, owned_fds

__all__ = ["clear_backend_caches"]

pytestmark = pytest.mark.usefixtures("clear_backend_caches")


def _assert_fds_closed(fds: list[int]) -> None:
    """Assert every descriptor in ``fds`` has been closed."""
    for fd in fds:
        with pytest.raises(OSError, match="Bad file descriptor"):
            os.fstat(fd)


class _GuardConstructionFailure:
    """Fail the blocking-mode guard constructor inside a whole hand-off.

    The acquisition duplicates the transport descriptors, so the worker pair
    shares their status flags exactly as the non-Linux worker pair does.
    ``engage`` therefore performs its real switch on descriptors the transport
    shares, and the injected fault lands only where the guard is built.
    """

    def __init__(self, failure: BaseException) -> None:
        """Record the failure to inject and start with empty observations."""
        self.failure = failure
        self.resume_calls = 0
        self.acquired: list[int] = []
        self.close_calls: dict[int, int] = {}
        self.collector = RecordingCollector()
        self._original_close_reader = (
            _pipeline_stream_native_cleanup._close_rust_reader_fd
        )
        self._original_close_writer = (
            _pipeline_stream_native_cleanup._close_rust_writer_fd
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the pause, drain, acquisition, close, and constructor seams."""

        def fail_construction(*_args: object, **_kwargs: object) -> typ.NoReturn:
            """Fail where the guard is built, after the real switch."""
            raise self.failure

        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            lambda _reader: _pipeline_stream_fds._ReaderPause(resume=self.resume),
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", mock.AsyncMock())
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.duplicate_transport_pair,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_close_rust_reader_fd",
            self.record_close_reader,
        )
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_close_rust_writer_fd",
            self.record_close_writer,
        )
        monkeypatch.setattr(
            _pipeline_stream_fds._BlockingModeGuard,
            "__init__",
            fail_construction,
        )

    def resume(self) -> None:
        """Record restoration of the paused reader."""
        self.resume_calls += 1

    def duplicate_transport_pair(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Duplicate the hand-off pair so the worker shares its status flags."""
        worker_reader_fd = os.dup(kwargs["reader_fd"])
        try:
            worker_writer_fd = os.dup(kwargs["writer_fd"])
        except BaseException:
            os.close(worker_reader_fd)
            raise
        self.acquired.extend((worker_reader_fd, worker_writer_fd))
        return _pipeline_stream_fds._NativePumpWorkerFds(
            reader_fd=worker_reader_fd,
            writer_fd=worker_writer_fd,
        )

    def record_close_reader(self, reader_fd: int) -> None:
        """Count the reader close, then close the descriptor once."""
        self.close_calls[reader_fd] = self.close_calls.get(reader_fd, 0) + 1
        self._original_close_reader(reader_fd)

    def record_close_writer(self, writer_fd: int) -> None:
        """Count the writer close, then close the descriptor once."""
        self.close_calls[writer_fd] = self.close_calls.get(writer_fd, 0) + 1
        self._original_close_writer(writer_fd)

    def run(self) -> bool:
        """Run the hand-off, asserting the modes settle before any fault travels."""
        with owned_fds() as (reader_fd, writer_fd):
            os.set_blocking(reader_fd, False)
            os.set_blocking(writer_fd, False)
            handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            )
            try:
                with observe_pump(PumpMetricsHook(self.collector)):
                    return asyncio.run(
                        _pipeline_streams._run_rust_pump(
                            reader=typ.cast("asyncio.StreamReader", object()),
                            writer=None,
                            handoff=handoff,
                        )
                    )
            finally:
                assert os.get_blocking(reader_fd) is False, (
                    "the guard-construction fault must restore the reader mode "
                    "the transport descriptor shares with its duplicate"
                )
                assert os.get_blocking(writer_fd) is False, (
                    "the guard-construction fault must restore the writer mode "
                    "the transport descriptor shares with its duplicate"
                )


def _assert_failed_activation_settled(harness: _GuardConstructionFailure) -> None:
    """Assert a failed activation closed once, resumed once, and settled."""
    assert len(harness.acquired) == 2, "acquisition must duplicate both descriptors"
    _assert_fds_closed(harness.acquired)
    assert harness.close_calls == dict.fromkeys(harness.acquired, 1), (
        "each duplicated descriptor must close exactly once"
    )
    assert harness.resume_calls == 1, (
        "a failed guard construction must resume the reader once"
    )
    assert harness.collector.gauge_values(RUST_PUMP_HELD_PAIRS) == [1.0, 0.0], (
        "a failed guard construction must still release the held-pair gauge"
    )


@pytest.mark.parametrize("failure_type", [RuntimeError, KeyboardInterrupt])
def test_a_guard_construction_fault_settles_the_handoff(
    failure_type: type[BaseException],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected guard-construction fault settles the pair and re-raises.

    The whole hand-off runs, so the fault lands on the real switch: the
    duplicated worker pair shares the transport's flags, ``engage`` restores
    both before the failure reaches the caller, and nothing the hand-off
    acquired stays open, paused, or held.
    """
    failure = failure_type("guard construction failed")
    harness = _GuardConstructionFailure(failure)
    harness.install(monkeypatch)

    with pytest.raises(failure_type) as raised:
        harness.run()

    assert raised.value is failure, (
        "the construction failure must reach the caller unchanged"
    )
    _assert_failed_activation_settled(harness)


@pytest.mark.parametrize("refusal", [OSError, ValueError])
def test_a_guard_construction_refusal_falls_back(
    refusal: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``OSError`` or ``ValueError`` at the guard falls back to Python.

    Blocking mode is the only capability this seam can decline, so the
    refusal must decline the hop rather than fail it, while still closing the
    duplicated pair, resuming the reader, and releasing the hold.
    """
    failure = refusal("blocking mode refused")
    harness = _GuardConstructionFailure(failure)
    harness.install(monkeypatch)

    handled = harness.run()

    assert handled is False, (
        "a blocking-mode refusal must let the Python pump carry the hop"
    )
    _assert_failed_activation_settled(harness)


class _FailedSettleSignalHarness:
    """Raise a shutdown signal from the hold release of a failed hand-off.

    A drain failure and an activation refusal settle a hold while an error is
    already travelling. Each path owes its outcome even when the release itself
    raises, so the harness raises on the ``-1`` transition alone.
    """

    def __init__(self, *, decline_activation: bool) -> None:
        """Choose which failed path precedes the interrupted release."""
        self.decline_activation = decline_activation
        self.resume_calls = 0
        self.acquired: list[int] = []
        self.events: list[PumpEvent] = []
        self._original_open = (
            _pipeline_stream_native_cleanup._open_native_pump_worker_fds
        )

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Install the pause, failing step, acquisition, and signal seams."""
        monkeypatch.setattr(
            _pipeline_streams,
            "_pause_reader_transport",
            lambda _reader: _pipeline_stream_fds._ReaderPause(resume=self.resume),
        )
        monkeypatch.setattr(_pipeline_streams, "_drain_reader_buffer", self.drain)
        monkeypatch.setattr(
            _pipeline_stream_native_cleanup,
            "_open_native_pump_worker_fds",
            self.record_open,
        )
        if self.decline_activation:
            monkeypatch.setattr(
                _pipeline_stream_native_cleanup,
                "_engage_rust_pump_blocking_mode",
                self.refuse_blocking_mode,
            )

    def resume(self) -> None:
        """Record restoration of the paused reader."""
        self.resume_calls += 1

    async def drain(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter | None,
    ) -> None:
        """Fail the buffered drain, or complete it before a refusal."""
        del reader, writer
        if self.decline_activation:
            await asyncio.sleep(0)
            return
        msg = "buffered drain failed"
        raise RuntimeError(msg)

    def record_open(
        self,
        **kwargs: int,
    ) -> _pipeline_stream_fds._NativePumpWorkerFds | None:
        """Record the worker pair the acquisition actually opened."""
        worker_fds = self._original_open(**kwargs)
        if worker_fds is not None:
            self.acquired.extend((worker_fds.reader_fd, worker_fds.writer_fd))
        return worker_fds

    def refuse_blocking_mode(
        self,
        reader_fd: int,
        writer_fd: int,
    ) -> typ.NoReturn:
        """Close the pair and decline blocking mode, as production does."""
        _pipeline_stream_fds._close_rust_reader_fd(reader_fd)
        _pipeline_stream_fds._close_rust_writer_fd(writer_fd)
        error = OSError("blocking mode refused")
        raise _pipeline_stream_native_cleanup._RustPumpBlockingModeError(error)

    def raise_signal(self, event: PumpEvent) -> None:
        """Raise the shutdown signal on the hold release alone."""
        if event.phase == "handoff_hold" and event.held_delta == -1:
            raise KeyboardInterrupt

    def run(self) -> None:
        """Run the failing hand-off, expecting the release signal to travel."""
        with owned_fds() as (reader_fd, writer_fd):
            handoff = _pipeline_stream_native_cleanup._RustPumpHandoff(
                reader_fd=reader_fd,
                writer_fd=writer_fd,
                cleanup_grace_s=0.5,
            )
            with (
                observe_pump(self.raise_signal),
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


@pytest.mark.parametrize(
    ("decline_activation", "expected_outcome"),
    [
        (False, RustPumpHandoffOutcome.DRAIN_FAILED),
        (True, RustPumpHandoffOutcome.BLOCKING_SETUP_FAILED),
    ],
)
def test_a_release_signal_during_a_failure_still_counts_its_outcome(
    decline_activation: bool,
    expected_outcome: RustPumpHandoffOutcome,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A signal at the hold release of a failed hand-off keeps its outcome.

    A drain failure and an activation refusal settle a hold while an error is
    already travelling, and each owes the counter its outcome even when the
    release itself raises the shutdown signal.
    """
    harness = _FailedSettleSignalHarness(decline_activation=decline_activation)
    harness.install(monkeypatch)

    harness.run()

    assert harness.resume_calls == 1, "the failure must resume the reader"
    assert len(harness.acquired) == 2, "acquisition must open both worker descriptors"
    _assert_fds_closed(harness.acquired)
    (outcome,) = [event.outcome for event in harness.events if event.phase == "handoff"]
    assert outcome is expected_outcome, (
        "the interrupted release must still count its outcome exactly once"
    )
