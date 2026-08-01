"""Backend-selection machinery for the tee hot-path profiling worker.

This module owns the stream-backend selection concern extracted from
``benchmarks.tee_profile_worker``: the ``BackendSelector`` and ``Clock``
protocols, the ``_EnvBackendSelector`` context manager, and the thread-local
reentrancy guard and observability metrics it depends on.

``_EnvBackendSelector`` is a deliberately non-reentrant context manager that
mutates the process-wide ``CUPRUM_STREAM_BACKEND`` environment variable while
holding ``_BACKEND_LOCK``. ``_BackendSelectorState`` provides the thread-local
reentrancy guard, while ``_BACKEND_LOCK`` is an ``RLock`` that serializes
``os.environ`` changes across workers and still allows same-thread helper code
to re-enter the lock safely. The selector clears the caches in
``cuprum._backend`` whenever it changes or restores ``CUPRUM_STREAM_BACKEND`` so
backend discovery reflects the active environment.

The module logger is named ``benchmarks.tee_profile_worker`` so log records and
tests continue to observe the worker's established logger name after the
extraction.
"""

from __future__ import annotations

import contextlib
import dataclasses as dc
import logging
import os
import threading
import time
import typing as typ

from benchmarks import BenchmarkError
from cuprum import _backend

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type BackendName = typ.Literal["auto", "python", "rust"]

_REENTRANT_SELECTOR_MESSAGE = (
    "_EnvBackendSelector is not re-entrant; nested calls are forbidden"
)
# Keep the worker's established logger name so log records and the tests and
# documentation that assert on it are unaffected by this extraction.
_logger = logging.getLogger("benchmarks.tee_profile_worker")


class ReentrantBackendSelectorError(BenchmarkError, RuntimeError):
    """Raised when ``_EnvBackendSelector`` is entered re-entrantly on a thread.

    Retains ``RuntimeError`` ancestry so existing callers that catch
    ``RuntimeError`` continue to work.

    Attributes
    ----------
    backend : BackendName
        The backend whose nested activation was rejected.
    thread_id : int
        Identifier of the thread that attempted the nested activation.
    rejection_count : int
        Running count of reentrant rejections recorded for the thread.
    """

    def __init__(
        self,
        *,
        backend: BackendName,
        thread_id: int,
        rejection_count: int,
    ) -> None:
        """Store the structured rejection context and set the error message."""
        self.backend = backend
        self.thread_id = thread_id
        self.rejection_count = rejection_count
        super().__init__(_REENTRANT_SELECTOR_MESSAGE)


class BackendSelector(typ.Protocol):
    """Interface for activating a named stream backend for a context.

    Implementations must be usable as a context manager that sets the backend
    on entry and restores the previous state on exit. Metrics state is exposed
    explicitly so worker result assembly does not depend on module globals.
    """

    @property
    def metrics_state(self) -> _MetricsState:
        """Return the metrics accumulator owned by this selector."""
        ...

    def __call__(self, backend: BackendName) -> contextlib.AbstractContextManager[None]:
        """Return a context manager that activates *backend*."""
        ...


class Clock(typ.Protocol):
    """Interface for wall-clock time measurement.

    The return value is a monotonically increasing float in seconds,
    compatible with ``time.perf_counter``.
    """

    def __call__(self) -> float:
        """Return the current time in seconds."""
        ...


_default_clock: Clock = time.perf_counter
_BACKEND_LOCK = threading.RLock()
_lock_state = threading.local()


class _BackendSelectorState:
    """Track selector activation for the current thread."""

    def __init__(self, state: threading.local) -> None:
        """Store the thread-local state object used by the selector."""
        self._state = state

    def enter(self) -> bool:
        """Mark this thread active, returning ``False`` for nested entry.

        Returns
        -------
        bool
            ``True`` if the thread became active, ``False`` on nested entry.
        """
        if self.is_active:
            return False
        self._state.is_active = True
        return True

    def exit(self) -> None:
        """Mark this thread inactive."""
        self._state.is_active = False

    @property
    def is_active(self) -> bool:
        """Return whether the current thread already owns the selector."""
        return bool(getattr(self._state, "is_active", False))


_selector_state = _BackendSelectorState(_lock_state)


@dc.dataclass(frozen=True, slots=True)
class _SelectorMetrics:
    """Thread-local selector observability metrics."""

    lock_wait_seconds: float = 0.0
    reentrant_rejection_count: int = 0


class _MetricsState:
    """Accumulate selector metrics for the current thread."""

    # Why keep this small wrapper instead of inlining thread-local fields:
    # CodeScene separately asked for explicit selector metrics ownership.
    # ``BackendSelector.metrics_state`` declares this type as the protocol
    # boundary, and property tests exercise its accumulation/reset invariants.
    # Removing it would hide the dependency again and drop that coverage.

    def __init__(self, state: threading.local) -> None:
        """Store the thread-local state object used by metrics."""
        self._state = state

    def reset(self) -> None:
        """Clear accumulated metrics for the current thread."""
        self._state.lock_wait_seconds = 0.0
        self._state.reentrant_rejection_count = 0

    def add_lock_wait(self, seconds: float) -> None:
        """Accumulate backend-lock wait duration."""
        self._state.lock_wait_seconds = self.snapshot().lock_wait_seconds + seconds

    def increment_rejections(self) -> int:
        """Increment and return the reentrant-rejection count.

        Returns
        -------
        int
            The updated reentrant-rejection count.
        """
        count = self.snapshot().reentrant_rejection_count + 1
        self._state.reentrant_rejection_count = count
        return count

    def snapshot(self) -> _SelectorMetrics:
        """Return current-thread selector metrics.

        Returns
        -------
        _SelectorMetrics
            A snapshot of the current thread's selector metrics.
        """
        return _SelectorMetrics(
            lock_wait_seconds=float(getattr(self._state, "lock_wait_seconds", 0.0)),
            reentrant_rejection_count=int(
                getattr(self._state, "reentrant_rejection_count", 0),
            ),
        )


class _EnvBackendSelector:
    """Activate a named stream backend by mutating ``os.environ``.

    Acquires ``_BACKEND_LOCK`` for the duration of the context to serialize
    access to ``os.environ`` and the backend LRU caches. Callers hold this
    context open for their entire repeat loop, so the lock serializes
    concurrent workers for that whole loop -- including each iteration's
    subprocess execution -- not merely the backend-selection step. This
    selector is not re-entrant; attempted nested entry raises
    ``RuntimeError`` and logs the rejected backend and thread identifier.
    The nested-entry error is ``ReentrantBackendSelectorError``, which
    retains ``RuntimeError`` ancestry so existing callers that catch
    ``RuntimeError`` still work.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        metrics_state: _MetricsState | None = None,
        selector_state: _BackendSelectorState | None = None,
    ) -> None:
        """Store selector collaborators used for timing and state tracking."""
        self._clock = clock if clock is not None else _default_clock
        self._metrics = (
            metrics_state
            if metrics_state is not None
            else _MetricsState(threading.local())
        )
        self._selector_state = (
            selector_state if selector_state is not None else _selector_state
        )

    @property
    def metrics_state(self) -> _MetricsState:
        """Return the metrics accumulator owned by this selector."""
        return self._metrics

    def __call__(
        self,
        backend: BackendName,
    ) -> contextlib.AbstractContextManager[None]:
        """Return a context manager that activates *backend*."""
        return self._activate(backend)

    @contextlib.contextmanager
    def _activate(self, backend: BackendName) -> cabc.Iterator[None]:
        """Select the stream backend for parent-side pipeline pumping."""
        lock_start = self._clock()
        with _BACKEND_LOCK:
            lock_end = self._clock()
            if not self._selector_state.enter():
                rejection_count = self._metrics.increment_rejections()
                _logger.warning(
                    "Rejected re-entrant backend selector activation: "
                    "backend=%r thread_id=%s selector_active=%s",
                    backend,
                    threading.get_ident(),
                    self._selector_state.is_active,
                )
                if rejection_count > 1:
                    _logger.error(
                        "Repeated re-entrant backend selector rejection: "
                        "backend=%r thread_id=%s reentrant_rejection_count=%s",
                        backend,
                        threading.get_ident(),
                        rejection_count,
                    )
                raise ReentrantBackendSelectorError(
                    backend=backend,
                    thread_id=threading.get_ident(),
                    rejection_count=rejection_count,
                )

            # Record the lock wait only for activations that actually enter, so
            # rejected re-entrant attempts do not inflate ``lock_wait_seconds``.
            self._metrics.add_lock_wait(lock_end - lock_start)
            try:
                previous = os.environ.get("CUPRUM_STREAM_BACKEND")
                try:
                    if backend == "auto":
                        os.environ.pop("CUPRUM_STREAM_BACKEND", None)
                    else:
                        os.environ["CUPRUM_STREAM_BACKEND"] = backend
                    _backend._check_rust_available.cache_clear()
                    _backend.get_stream_backend.cache_clear()
                    yield
                finally:
                    if previous is None:
                        os.environ.pop("CUPRUM_STREAM_BACKEND", None)
                    else:
                        os.environ["CUPRUM_STREAM_BACKEND"] = previous
                    _backend._check_rust_available.cache_clear()
                    _backend.get_stream_backend.cache_clear()
            finally:
                self._selector_state.exit()
