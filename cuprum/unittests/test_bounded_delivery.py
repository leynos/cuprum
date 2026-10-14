"""Byte bounds, worker shutdown, and public bounded-delivery options."""

from __future__ import annotations

import io
import math
import threading
import time
import typing as typ

import pytest

import cuprum
from cuprum._bounded_delivery import (
    _DeliveryItem,
    _DeliveryPayload,
    _DeliveryWorker,
)
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum.echo_events import EchoStream
from cuprum.sh import BoundedDelivery, RunOutputOptions
from cuprum.unittests._sink_test_support import BlockedSink


def _delivery_worker(
    sink: typ.IO[str],
    *,
    max_queued_bytes: int = 4,
) -> _DeliveryWorker:
    """Create a worker whose two streams share the same test destination."""
    return _DeliveryWorker(
        destinations={EchoStream.STDOUT: sink, EchoStream.STDERR: sink},
        max_queued_bytes=max_queued_bytes,
    )


def _payload(value: str, source_bytes: int) -> _DeliveryPayload:
    """Build one prepared text payload with raw source accounting."""
    return _DeliveryPayload(value=value, source_bytes=source_bytes)


def test_worker_bounds_queued_source_bytes_and_keeps_one_shared_fifo() -> None:
    """Queue saturation drops incoming data without changing stream order."""
    sink = BlockedSink()
    worker = _delivery_worker(sink)
    try:
        assert (
            worker.enqueue(
                EchoStream.STDOUT,
                _payload("first", 2),
                flush=True,
            )
            == 0
        )
        assert sink.started.wait(timeout=1)
        assert (
            worker.enqueue(
                EchoStream.STDERR,
                _payload("éé", 2),
                flush=True,
            )
            == 0
        )
        assert worker.queued_bytes == 2
        assert (
            worker.enqueue(
                EchoStream.STDOUT,
                _payload("drop", 3),
                flush=True,
            )
            == 3
        )
        assert worker.queued_bytes == 2
    finally:
        sink.release.set()
    result = worker.close(grace=1)

    assert result.failures == ()
    assert result.undelivered_bytes == ()
    assert sink.getvalue() == "firstéé"


@pytest.mark.parametrize("failure_site", ["write", "flush"])
def test_worker_disables_after_write_or_flush_failure(failure_site: str) -> None:
    """A sink exception stays categorical and pending bytes become undelivered."""
    ready = threading.Event()
    release = threading.Event()

    class _FailingSink(io.StringIO):
        """Raise during write or flush to exercise failure reporting."""

        def write(self, text: str, /) -> int:
            """Block, then raise when the test selects a write failure."""
            ready.set()
            if not release.wait(timeout=3):
                raise RuntimeError
            if failure_site == "write":
                raise RuntimeError
            return super().write(text)

        def flush(self) -> None:
            """Raise when the test selects a flush failure."""
            if failure_site == "flush":
                raise RuntimeError

    sink = _FailingSink()
    worker = _delivery_worker(sink)
    stdout_diagnostics = _RelayDiagnostics()
    stderr_diagnostics = _RelayDiagnostics()
    assert (
        worker.enqueue(
            EchoStream.STDOUT,
            _payload("one", 1),
            flush=True,
            diagnostics=stdout_diagnostics,
        )
        == 0
    )
    assert ready.wait(timeout=1)
    assert (
        worker.enqueue(
            EchoStream.STDERR,
            _payload("two", 2),
            flush=True,
            diagnostics=stderr_diagnostics,
        )
        == 0
    )
    release.set()
    result = worker.close(grace=1)

    assert tuple(failure.stream for failure in result.failures) == (EchoStream.STDOUT,)
    assert result.failures[0].diagnostics is stdout_diagnostics
    assert tuple(
        (item.stream, item.byte_count) for item in result.undelivered_bytes
    ) == ((EchoStream.STDERR, 2),)
    assert result.undelivered_bytes[0].diagnostics is stderr_diagnostics
    assert worker.close(grace=1) == result


def test_worker_contains_base_exception_and_accounts_queued_output() -> None:
    """Sink exits disable delivery and retain only categorical diagnostics."""
    ready = threading.Event()
    release = threading.Event()

    class _SinkAbort(BaseException):
        """Represent a sink raising outside the ordinary exception tree."""

    class _AbortingSink(io.StringIO):
        """Block the active write before raising a BaseException subclass."""

        def write(self, text: str, /) -> int:
            ready.set()
            if not release.wait(timeout=3):
                raise RuntimeError
            raise _SinkAbort

    worker = _delivery_worker(_AbortingSink())
    assert (
        worker.enqueue(
            EchoStream.STDOUT,
            _payload("active", 2),
            flush=True,
        )
        == 0
    )
    assert ready.wait(timeout=1)
    assert (
        worker.enqueue(
            EchoStream.STDERR,
            _payload("queued", 3),
            flush=True,
        )
        == 0
    )
    release.set()

    result = worker.close(grace=1)

    assert tuple(failure.stream for failure in result.failures) == (EchoStream.STDOUT,)
    assert tuple(
        (item.stream, item.byte_count) for item in result.undelivered_bytes
    ) == ((EchoStream.STDERR, 3),)
    assert worker.queued_bytes == 0


def test_close_accounts_for_active_item_left_by_a_stopped_worker() -> None:
    """Close repairs accounting if a worker exits with an active item set."""
    worker = _delivery_worker(io.StringIO())
    with worker._condition:
        worker._active_item = _DeliveryItem(
            stream=EchoStream.STDOUT,
            payload=_payload("stuck", 2),
            flush=True,
            diagnostics=None,
        )
    result = worker.close(grace=1)

    assert tuple(
        (item.stream, item.byte_count) for item in result.undelivered_bytes
    ) == ((EchoStream.STDOUT, 2),)
    assert worker.queued_bytes == 0


def test_close_abandons_a_blocked_daemon_after_the_drain_grace() -> None:
    """Shutdown reports active and queued bytes without joining indefinitely."""
    sink = BlockedSink()
    worker = _delivery_worker(sink)
    assert (
        worker.enqueue(
            EchoStream.STDOUT,
            _payload("active", 2),
            flush=True,
        )
        == 0
    )
    assert sink.started.wait(timeout=1)
    assert (
        worker.enqueue(
            EchoStream.STDERR,
            _payload("queued", 3),
            flush=True,
        )
        == 0
    )

    started_at = time.monotonic()
    result = worker.close(grace=0.01)
    elapsed = time.monotonic() - started_at

    assert elapsed < 0.5
    assert worker._thread.daemon is True
    assert worker._thread.name == "cuprum-output-delivery"
    assert worker._thread.is_alive()
    assert tuple(
        (item.stream, item.byte_count) for item in result.undelivered_bytes
    ) == ((EchoStream.STDOUT, 2), (EchoStream.STDERR, 3))
    sink.release.set()
    worker._thread.join(timeout=1)
    assert not worker._thread.is_alive()


def test_bounded_delivery_is_keyword_only_and_exported_from_both_packages() -> None:
    """The new option is public without taking an existing positional slot."""
    assert cuprum.BoundedDelivery is BoundedDelivery
    assert RunOutputOptions(delivery=BoundedDelivery(16)).delivery == BoundedDelivery(
        16,
        None,
    )
    assert "delivery" not in RunOutputOptions.__match_args__


@pytest.mark.parametrize(
    ("max_queued_bytes", "drain_grace_s", "error_type"),
    [
        (0, None, ValueError),
        (-1, None, ValueError),
        (True, None, TypeError),
        ("4", None, TypeError),
        (4, "0.1", TypeError),
        (4, -0.1, ValueError),
        (4, math.inf, ValueError),
        (4, math.nan, ValueError),
    ],
)
def test_run_output_options_validates_bounded_delivery_values(
    max_queued_bytes: object,
    drain_grace_s: object,
    error_type: type[Exception],
) -> None:
    """Invalid queue capacities and drain grace values fail at construction."""
    delivery = BoundedDelivery(
        max_queued_bytes=typ.cast("int", max_queued_bytes),
        drain_grace_s=typ.cast("float | None", drain_grace_s),
    )
    with pytest.raises(error_type):
        RunOutputOptions(delivery=delivery)


def test_run_output_options_rejects_a_wrong_delivery_type() -> None:
    """The execution options reject unrelated values before a run starts."""
    with pytest.raises(TypeError, match="BoundedDelivery"):
        RunOutputOptions(delivery=typ.cast("BoundedDelivery", object()))
