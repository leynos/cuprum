"""Run-owned, byte-bounded delivery for blocking output destinations.

The event loop enqueues prepared output without waiting for a destination.
One daemon worker writes stdout and stderr through a shared FIFO, and the
run collects only categorical failures and byte counts when it closes the
worker.
"""

from __future__ import annotations

import collections
import dataclasses as dc
import math
import threading
import typing as typ

if typ.TYPE_CHECKING:
    from cuprum._stream_drain_state import _RelayDiagnostics
    from cuprum.echo_events import EchoStream

_DELIVERY_DRAIN_GRACE_S = 0.25

type _DeliveryDestination = typ.IO[str] | typ.BinaryIO
type _DeliveryValue = str | bytes


@dc.dataclass(frozen=True, slots=True)
class BoundedDelivery:
    """Select byte-bounded, off-loop delivery for echoed output.

    Parameters
    ----------
    max_queued_bytes : int
        Positive upper bound for payload bytes waiting in the run's shared
        stdout/stderr delivery queue. A chunk that does not fit is dropped
        from presentation and reported; capture remains complete.
    drain_grace_s : float | None, default=None
        Maximum time spent draining the worker during shutdown. ``None`` uses
        the library default. A blocked writer is abandoned as a daemon thread
        after this grace; Python cannot forcibly stop a running thread.

    Examples
    --------
    >>> delivery = BoundedDelivery(max_queued_bytes=256 * 1024)
    >>> delivery.max_queued_bytes
    262144
    """

    max_queued_bytes: int
    drain_grace_s: float | None = None


@dc.dataclass(frozen=True, slots=True)
class _DeliveryPayload:
    """Prepared presentation data with its original byte accounting."""

    value: _DeliveryValue
    source_bytes: int


@dc.dataclass(frozen=True, slots=True)
class _DeliveryItem:
    """One ordered sink operation waiting for the delivery worker."""

    stream: EchoStream
    payload: _DeliveryPayload
    flush: bool
    diagnostics: _RelayDiagnostics | None


@dc.dataclass(frozen=True, slots=True)
class _DeliveryFailure:
    """A destination failure without its exception or payload details."""

    stream: EchoStream
    diagnostics: _RelayDiagnostics | None


@dc.dataclass(frozen=True, slots=True)
class _UndeliveredBytes:
    """Unconfirmed output grouped by the drain that queued it."""

    stream: EchoStream
    byte_count: int
    diagnostics: _RelayDiagnostics | None


@dc.dataclass(frozen=True, slots=True)
class _DeliveryCloseResult:
    """Diagnostics collected when a run closes its delivery worker."""

    failures: tuple[_DeliveryFailure, ...]
    undelivered_bytes: tuple[_UndeliveredBytes, ...]


def _validate_delivery_options(
    delivery: object,
) -> BoundedDelivery | None:
    """Validate and normalize a bounded-delivery option object."""
    if delivery is None:
        return None
    if not isinstance(delivery, BoundedDelivery):
        msg = "RunOutputOptions delivery must be a BoundedDelivery or None"
        raise TypeError(msg)
    _validate_queue_capacity(delivery.max_queued_bytes)
    drain_grace_s = _normalize_drain_grace(delivery.drain_grace_s)
    if drain_grace_s is None:
        return delivery
    return BoundedDelivery(delivery.max_queued_bytes, drain_grace_s)


def _validate_queue_capacity(max_queued_bytes: object) -> None:
    """Require a strictly positive integer queue capacity."""
    if isinstance(max_queued_bytes, bool) or not isinstance(max_queued_bytes, int):
        msg = "BoundedDelivery max_queued_bytes must be an integer"
        raise TypeError(msg)
    if max_queued_bytes <= 0:
        msg = "BoundedDelivery max_queued_bytes must be positive"
        raise ValueError(msg)


def _normalize_drain_grace(drain_grace_s: object) -> float | None:
    """Validate an optional finite drain grace and normalize it to float."""
    if drain_grace_s is None:
        return None
    if isinstance(drain_grace_s, bool) or not isinstance(drain_grace_s, int | float):
        msg = "BoundedDelivery drain_grace_s must be a finite number or None"
        raise TypeError(msg)
    try:
        grace = float(drain_grace_s)
    except OverflowError as exc:
        msg = "BoundedDelivery drain_grace_s must be finite"
        raise ValueError(msg) from exc
    if not math.isfinite(grace) or grace < 0:
        msg = "BoundedDelivery drain_grace_s must be finite and non-negative"
        raise ValueError(msg)
    return grace


@dc.dataclass(slots=True, init=False)
class _DeliveryWorker:
    """One run-owned daemon worker with a bounded shared stream queue."""

    destinations: dict[EchoStream, _DeliveryDestination]
    max_queued_bytes: int
    _queue: collections.deque[_DeliveryItem]
    _condition: threading.Condition
    _thread: threading.Thread
    _queued_bytes: int
    _active_item: _DeliveryItem | None
    _closing: bool
    _abandoned: bool
    _disabled: bool
    _failures: list[_DeliveryFailure]
    _undelivered_by_target: list[_UndeliveredBytes]
    _close_result: _DeliveryCloseResult | None
    _diagnostics_reported: bool

    def __init__(
        self,
        destinations: dict[EchoStream, _DeliveryDestination],
        max_queued_bytes: int,
    ) -> None:
        """Start one daemon worker for the resolved per-stream destinations."""
        self.destinations = dict(destinations)
        self.max_queued_bytes = max_queued_bytes
        self._queue = collections.deque()
        self._condition = threading.Condition(threading.Lock())
        self._queued_bytes = 0
        self._active_item = None
        self._closing = False
        self._abandoned = False
        self._disabled = False
        self._failures = []
        self._undelivered_by_target = []
        self._close_result = None
        self._diagnostics_reported = False
        self._thread = threading.Thread(
            target=self._run,
            name="cuprum-output-delivery",
            daemon=True,
        )
        self._thread.start()

    @property
    def queued_bytes(self) -> int:
        """The number of retained payload bytes awaiting delivery."""
        with self._condition:
            return self._queued_bytes

    @property
    def failures(self) -> tuple[_DeliveryFailure, ...]:
        """The destination failures recorded by the worker."""
        with self._condition:
            return tuple(self._failures)

    @property
    def undelivered_bytes(self) -> tuple[_UndeliveredBytes, ...]:
        """The accumulated unconfirmed output grouped by stream drain."""
        with self._condition:
            return self._undelivered_snapshot()

    def enqueue(
        self,
        stream: EchoStream,
        payload: _DeliveryPayload,
        *,
        flush: bool,
        diagnostics: _RelayDiagnostics | None = None,
    ) -> int:
        """Queue one write without blocking and return bytes rejected by load."""
        byte_count = payload.source_bytes
        if byte_count == 0 and not payload.value:
            return 0
        with self._condition:
            if self._disabled or self._closing:
                self._add_undelivered(stream, byte_count, diagnostics)
                return 0
            if self._queued_bytes + byte_count > self.max_queued_bytes:
                return byte_count
            self._queue.append(
                _DeliveryItem(stream, payload, flush, diagnostics),
            )
            self._queued_bytes += byte_count
            self._condition.notify()
        return 0

    def close(self, grace: float = _DELIVERY_DRAIN_GRACE_S) -> _DeliveryCloseResult:
        """Drain for at most *grace*, then abandon a still-blocked daemon."""
        with self._condition:
            if self._close_result is not None:
                return self._close_result
            self._closing = True
            self._condition.notify_all()
        self._thread.join(grace)
        with self._condition:
            if self._thread.is_alive():
                self._abandoned = True
                if self._active_item is not None:
                    self._add_undelivered(
                        self._active_item.stream,
                        self._active_item.payload.source_bytes,
                        self._active_item.diagnostics,
                    )
                self._discard_queued()
                self._condition.notify_all()
            self._close_result = _DeliveryCloseResult(
                failures=tuple(self._failures),
                undelivered_bytes=self._undelivered_snapshot(),
            )
            return self._close_result

    def take_close_result(self) -> _DeliveryCloseResult | None:
        """Return close diagnostics once, after close has completed."""
        with self._condition:
            if self._close_result is None or self._diagnostics_reported:
                return None
            self._diagnostics_reported = True
            return self._close_result

    def _run(self) -> None:
        """Write queued items in FIFO order until close or permanent failure."""
        while (item := self._next_item()) is not None:
            try:
                self._deliver(item)
            except Exception:  # ruff: ignore[blind-except] - sink errors stay categorical.
                with self._condition:
                    if self._abandoned:
                        return
                    self._failures.append(
                        _DeliveryFailure(
                            stream=item.stream,
                            diagnostics=item.diagnostics,
                        ),
                    )
                    self._disabled = True
                    self._active_item = None
                    self._discard_queued()
                    self._condition.notify_all()
                return
            with self._condition:
                self._active_item = None
                if self._abandoned:
                    return
                self._condition.notify_all()

    def _next_item(self) -> _DeliveryItem | None:
        """Take the next FIFO item or stop after a drained close."""
        with self._condition:
            while not self._queue and not self._closing:
                self._condition.wait()
            if self._abandoned or not self._queue:
                return None
            item = self._queue.popleft()
            self._queued_bytes -= item.payload.source_bytes
            self._active_item = item
            return item

    def _deliver(self, item: _DeliveryItem) -> None:
        """Perform one blocking destination write and its optional flush."""
        destination = self.destinations[item.stream]
        if item.payload.value:
            if isinstance(item.payload.value, bytes):
                target = getattr(destination, "buffer", destination)
                typ.cast("typ.BinaryIO", target).write(item.payload.value)
            else:
                typ.cast("typ.IO[str]", destination).write(item.payload.value)
        if item.flush:
            destination.flush()

    def _discard_queued(self) -> None:
        """Forget every pending item and account for its bytes as undelivered."""
        while self._queue:
            item = self._queue.popleft()
            self._add_undelivered(
                item.stream,
                item.payload.source_bytes,
                item.diagnostics,
            )
        self._queued_bytes = 0

    def _add_undelivered(
        self,
        stream: EchoStream,
        byte_count: int,
        diagnostics: _RelayDiagnostics | None,
    ) -> None:
        """Accumulate unconfirmed bytes for the stream that produced them."""
        if byte_count == 0:
            return
        for index, entry in enumerate(self._undelivered_by_target):
            if entry.stream is stream and entry.diagnostics is diagnostics:
                self._undelivered_by_target[index] = dc.replace(
                    entry,
                    byte_count=entry.byte_count + byte_count,
                )
                return
        self._undelivered_by_target.append(
            _UndeliveredBytes(stream, byte_count, diagnostics),
        )

    def _undelivered_snapshot(self) -> tuple[_UndeliveredBytes, ...]:
        """Return stable queue order for result and event emission."""
        return tuple(self._undelivered_by_target)
