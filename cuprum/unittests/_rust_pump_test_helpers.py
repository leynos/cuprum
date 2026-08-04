"""Shared support for tests that observe Rust-pump routing decisions.

Both the log-record tests and the metrics tests have to reach the *real*
decline paths rather than calling the recording helper directly — a helper
called by hand proves only that the helper works, not that the pump still calls
it. The triggers below therefore drive ``_pump_over_raw_fds`` and
``_try_rust_pump`` with exactly the descriptor state each seam refuses, so
deleting the call site fails every test that uses them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import threading
import types
import typing as typ

from cuprum import _pipeline_stream_fds, _pipeline_streams

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest


class RecordingCollector:
    """Metrics collector double that keeps every call with its labels.

    ``InMemoryMetrics`` discards labels, which is exactly the dimension these
    tests exist to pin, so the label mapping is copied per call here.
    """

    def __init__(self) -> None:
        """Start with no recorded counter or histogram calls."""
        self.counters: list[tuple[str, float, dict[str, str]]] = []
        self.histograms: list[tuple[str, float, dict[str, str]]] = []

    def inc_counter(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a counter increment and the labels it carried."""
        self.counters.append((name, value, dict(labels)))

    def observe_histogram(
        self,
        name: str,
        value: float,
        labels: cabc.Mapping[str, str],
    ) -> None:
        """Record a histogram observation and the labels it carried."""
        self.histograms.append((name, value, dict(labels)))

    def counter_names(self) -> list[str]:
        """Return the names of the counters recorded, in call order."""
        return [name for name, _value, _labels in self.counters]


def fail_engage(**_kwargs: object) -> object:
    """Refuse to switch the descriptors to blocking mode."""
    msg = "blocking mode is unavailable for this descriptor pair"
    raise OSError(msg)


@contextlib.contextmanager
def _owned_fds() -> cabc.Iterator[tuple[int, int]]:
    """Yield a private pipe's descriptors, closing both on exit.

    The pump seams are doubled out in every path below, but only by
    convention: ``_pause_reader_transport`` reports ``may_hand_off=True`` for a
    reader with no transport, so a caller that forgets one patch reaches
    ``_BlockingModeGuard.engage`` for real. Against descriptors 1 and 2 that is
    an ``fcntl`` on the test runner's own stdout and stderr, and the damage
    surfaces far from here. Descriptors this helper owns make the same mistake
    harmless.
    """
    reader_fd, writer_fd = os.pipe()
    try:
        yield reader_fd, writer_fd
    finally:
        for fd in (reader_fd, writer_fd):
            # The Rust pump closes the writer on return, so a real hand-off may
            # already have taken one of these.
            with contextlib.suppress(OSError):
                os.close(fd)


def allow_pause(monkeypatch: pytest.MonkeyPatch, *, may_hand_off: bool) -> None:
    """Force the reader-pause seam to the given hand-off verdict."""
    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=may_hand_off),
    )


def decline_on_missing_fds(_monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a hop whose streams expose no raw descriptors."""
    reader = typ.cast("asyncio.StreamReader", object())
    asyncio.run(_pipeline_streams._try_rust_pump(reader, None))


def decline_on_pause_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a hop whose reader transport cannot be paused."""
    allow_pause(monkeypatch, may_hand_off=False)
    run_raw_fd_pump()


def decline_on_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a hop whose descriptors cannot be made blocking."""
    allow_pause(monkeypatch, may_hand_off=True)
    monkeypatch.setattr(_pipeline_stream_fds._BlockingModeGuard, "engage", fail_engage)
    run_raw_fd_pump()


def run_raw_fd_pump() -> bool:
    """Drive ``_pump_over_raw_fds`` over a pipe this helper owns."""
    reader = typ.cast("asyncio.StreamReader", object())
    with _owned_fds() as (reader_fd, writer_fd):
        return asyncio.run(
            _pipeline_streams._pump_over_raw_fds(
                reader=reader,
                writer=None,
                reader_fd=reader_fd,
                writer_fd=writer_fd,
            )
        )


class _NoopGuard:
    """Blocking-mode guard double whose restore does nothing."""

    def restore(self) -> None:
        """Restore nothing; the descriptors here are placeholders."""


def hand_off_successfully(monkeypatch: pytest.MonkeyPatch) -> bool:
    """Drive a hop that the Rust pump accepts and completes.

    Every seam the decline paths exercise is made to succeed, so the hop
    reaches the pump and returns without a decline. This is the negative
    control: whatever a declined hop records, this must not.
    """
    allow_pause(monkeypatch, may_hand_off=True)
    monkeypatch.setattr(
        _pipeline_stream_fds._BlockingModeGuard,
        "engage",
        lambda **_kwargs: _NoopGuard(),
    )
    install_fake_pump(monkeypatch, lambda _reader_fd, _writer_fd: 0)
    return run_raw_fd_pump()


def install_fake_pump(
    monkeypatch: pytest.MonkeyPatch,
    pump: cabc.Callable[[int, int], int],
) -> None:
    """Replace the Rust pump entry point with ``pump``."""
    fake_streams_rs = types.ModuleType("cuprum._streams_rs")
    fake_streams_rs.rust_pump_stream = pump  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "cuprum._streams_rs", fake_streams_rs)


async def cancel_mid_transfer(
    worker_started: threading.Event,
    release: threading.Event,
) -> None:
    """Start the pump, cancel it mid-transfer, then release the worker."""
    with _owned_fds() as (reader_fd, writer_fd):
        await _drive_cancelled_pump(
            worker_started,
            release,
            reader_fd=reader_fd,
            writer_fd=writer_fd,
        )


async def _drive_cancelled_pump(
    worker_started: threading.Event,
    release: threading.Event,
    *,
    reader_fd: int,
    writer_fd: int,
) -> None:
    """Cancel an in-flight pump over ``reader_fd``/``writer_fd``."""
    state = _pipeline_streams._RustPumpState(
        reader_fd=reader_fd,
        writer_fd=writer_fd,
        blocking_mode_guard=typ.cast("_pipeline_stream_fds._BlockingModeGuard", _NoopGuard()),
        resume_reader=None,
    )
    task = asyncio.create_task(
        _pipeline_streams._run_rust_pump_with_blocking_fds(state=state),
    )
    # `Event.wait` reports timeout by returning False rather than raising, so a
    # discarded result would let the cancellation land before the worker ever
    # started — passing without exercising mid-transfer cancellation at all.
    started = await asyncio.to_thread(worker_started.wait, 5.0)
    assert started, (
        "the pump worker did not start within 5s, so the cancellation below "
        "would not be mid-transfer"
    )
    task.cancel()
    await asyncio.sleep(0.05)
    release.set()
    try:
        await task
    except asyncio.CancelledError:
        return
    msg = "the cancelled hop must report the cancellation to its caller"
    raise AssertionError(msg)


def run_failing_pump_on_a_cancelled_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cancel a hop whose Rust worker then fails, so the failure is recovered."""
    release = threading.Event()
    worker_started = threading.Event()

    def failing_pump(reader_fd: int, writer_fd: int) -> int:
        """Fail after the cancellation has been delivered."""
        del reader_fd, writer_fd
        worker_started.set()
        release.wait(timeout=5.0)
        msg = "the pump failed while the hop was being cancelled"
        raise OSError(msg)

    install_fake_pump(monkeypatch, failing_pump)
    asyncio.run(cancel_mid_transfer(worker_started, release))


DECLINE_PATHS: tuple[
    tuple[str, cabc.Callable[[pytest.MonkeyPatch], None], str], ...
] = (
    ("missing_fds", decline_on_missing_fds, "raw_fd_unavailable"),
    ("pause_failure", decline_on_pause_failure, "reader_pause_failed"),
    ("blocking_failure", decline_on_blocking_failure, "blocking_mode_unavailable"),
)
"""Each real decline path paired with the ``reason`` it must report."""
