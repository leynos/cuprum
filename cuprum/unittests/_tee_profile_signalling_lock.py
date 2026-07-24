"""A contention-signalling ``RLock`` wrapper for tee-profile concurrency tests.

Isolated from the worker/selector harness in
``_tee_profile_concurrency_support`` so the instrumented lock primitive has a
single, focused home.
"""

from __future__ import annotations

import typing as typ

if typ.TYPE_CHECKING:
    import threading
    import types


class _RLockLike(typ.Protocol):
    """Structural type for the ``RLock`` operations this test instruments."""

    def acquire(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the lock, optionally blocking up to ``timeout`` seconds."""
        ...

    def release(self) -> None:
        """Release the lock."""
        ...


class _SignallingRLock:
    """Signal when a blocking acquire observes lock contention."""

    def __init__(
        self,
        delegate: _RLockLike,
        contention_event: threading.Event,
    ) -> None:
        """Store the wrapped lock and contention signal.

        Parameters
        ----------
        delegate
            Lock object that receives all actual acquire and release calls.
        contention_event
            Event set after a blocking acquire attempt observes that the lock is
            already held.
        """
        self._delegate = delegate
        self._contention_event = contention_event

    def acquire(self, *, blocking: bool = True, timeout: float = -1) -> bool:
        """Acquire the wrapped lock, signalling first observed contention.

        Parameters
        ----------
        blocking
            Whether the wrapped acquire may block.
        timeout
            Maximum wait passed to the wrapped lock.

        Returns
        -------
        bool
            The wrapped lock acquisition result.
        """
        if not blocking:
            return self._delegate.acquire(blocking=False)

        if self._delegate.acquire(blocking=False):
            self._delegate.release()
        else:
            self._contention_event.set()

        if timeout == -1:
            return self._delegate.acquire(blocking=True)
        return self._delegate.acquire(blocking=True, timeout=timeout)

    def release(self) -> None:
        """Release the wrapped lock."""
        self._delegate.release()

    def __enter__(self) -> _SignallingRLock:
        """Acquire the wrapped lock on context entry and return self."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: types.TracebackType | None,
    ) -> None:
        """Release the wrapped lock on context exit."""
        self.release()
