"""State and resource models shared by native-pump lifecycle helpers."""

from __future__ import annotations

import dataclasses as dc
import threading
import time
import typing as typ

from cuprum.pump_span_observation import _EMPTY_PUMP_HOP_SPANS

if typ.TYPE_CHECKING:
    import asyncio
    import collections.abc as cabc
    import contextvars

    from cuprum._pipeline_native_pump_runtime import _NativePumpRuntime
    from cuprum._pipeline_stream_fds import _BlockingModeGuard
    from cuprum.pump_span_observation import _PumpHopSpans


_DEFAULT_NATIVE_PUMP_CLEANUP_GRACE = 0.5


@dc.dataclass(slots=True)
class _RustPumpState:
    """Capture callback-owned duplicates that native pumping must restore."""

    reader_fd: int
    writer_fd: int
    blocking_mode_guard: _BlockingModeGuard
    resume_reader: cabc.Callable[[], None] | None
    was_cancelled: bool = False
    monotonic_clock: cabc.Callable[[], float] = time.monotonic
    cleanup_grace_s: float = _DEFAULT_NATIVE_PUMP_CLEANUP_GRACE
    was_deferred: bool = False
    _cleanup_lock: threading.Lock = dc.field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _cleanup_completed: bool = False

    def defer_cleanup(self) -> bool:
        """Atomically defer callback cleanup unless completion has already won."""
        with self._cleanup_lock:
            if self._cleanup_completed:
                return False
            self.was_deferred = True
            return True

    def complete_cleanup(self) -> bool:
        """Finish callback cleanup and return whether grace expiry won first."""
        with self._cleanup_lock:
            self._cleanup_completed = True
            return self.was_deferred


@dc.dataclass(frozen=True, slots=True)
class _NativePumpFds:
    """Duplicated descriptors whose lifetime belongs to native pumping."""

    reader_fd: int
    writer_fd: int


@dc.dataclass(frozen=True, slots=True)
class _RustPumpCompletion:
    """Retain callback-owned resources until native pumping has settled."""

    cleanup_complete: asyncio.Future[None]
    native_fds: _NativePumpFds
    state: _RustPumpState
    runtime: _NativePumpRuntime
    completion_context: contextvars.Context
    pump_hop_spans: _PumpHopSpans = _EMPTY_PUMP_HOP_SPANS


@dc.dataclass(frozen=True, slots=True)
class _RustPumpHandoff:
    """Raw descriptors and caller-facing cleanup policy for one hop."""

    reader_fd: int
    writer_fd: int
    cleanup_grace_s: float


class _RustPumpStateDuplicationError(Exception):
    """Retain a state-duplication failure for the caller to turn into fallback.

    Duplication is best-effort: the descriptors were extracted moments earlier,
    so a transport asyncio closed in between makes this fail on a hop that would
    otherwise have taken the fast path. It is a decline, not a fault — raising
    it used to leave the writer transport open and wedge the pipeline.
    """

    def __init__(self, error: OSError | ValueError) -> None:
        """Store the original descriptor duplication failure."""
        super().__init__(str(error))
        self.error = error


class _RustPumpBlockingModeError(Exception):
    """Retain a blocking-mode refusal for the caller to turn into fallback."""

    def __init__(self, error: OSError | ValueError) -> None:
        """Store the original blocking-mode refusal."""
        super().__init__(str(error))
        self.error = error
