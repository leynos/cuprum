"""Stream and ``os`` doubles shared by the exit-status guard tests.

The doubles stand in for the standard streams and for the ``os`` calls that
redirect a descriptor, so each test can state exactly which failure the guard
should see without touching the real standard streams of the test process.
"""

from __future__ import annotations

import os
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import types


class _FakeStream:
    """A standard-stream double that records what the hook asks of it.

    Attributes
    ----------
    flushes:
        How many times ``flush`` was called.
    """

    def __init__(
        self,
        *,
        flush_error: BaseException | None = None,
        descriptor: int | None = None,
        fileno_error: BaseException | None = None,
    ) -> None:
        """Configure the failure each call reports, if any."""
        self.flushes = 0
        self._flush_error = flush_error
        self._descriptor = descriptor
        self._fileno_error = fileno_error

    def flush(self) -> None:
        """Record the flush, then raise the configured failure."""
        self.flushes += 1
        if self._flush_error is not None:
            raise self._flush_error

    def fileno(self) -> int:
        """Return the configured descriptor, or raise the configured failure."""
        if self._fileno_error is not None:
            raise self._fileno_error
        if self._descriptor is None:
            msg = "test double has no descriptor"
            raise AssertionError(msg)
        return self._descriptor


class _DeadProxy:
    """A standard-stream proxy whose reader has gone away."""

    @staticmethod
    def flush() -> None:
        """Report the reader as gone, as a broken proxy would."""
        raise BrokenPipeError


class _ProxyWithoutFileno(_DeadProxy):
    """A dead proxy that never had a ``fileno`` method."""


class _ProxyReturningNoDescriptor(_DeadProxy):
    """A dead proxy whose ``fileno`` reports no descriptor."""

    def fileno(self) -> None:
        """Return no descriptor at all."""


class _ProxyWithNonCallableFileno(_DeadProxy):
    """A dead proxy whose ``fileno`` attribute is not callable."""

    fileno = None


class _RecordingAtexit:
    """A stand-in for the ``atexit`` module that records registrations."""

    def __init__(self) -> None:
        """Start with nothing registered."""
        self.registered: list[object] = []

    def register(self, func: object, /, *args: object, **kwargs: object) -> None:
        """Record one registration without installing it."""
        self.registered.append(func)


class _OsProxy:
    """An ``os`` stand-in that delegates whatever it does not override."""

    def __init__(self, real: types.ModuleType) -> None:
        """Wrap the real module so every other attribute still works."""
        self._real = real

    def __getattr__(self, name: str) -> object:
        """Delegate every attribute the guard does not override."""
        return getattr(self._real, name)


class _FailingDup2Os(_OsProxy):
    """An ``os`` proxy whose ``dup2`` always fails."""

    def __init__(self, real: types.ModuleType) -> None:
        """Start with no redirect attempts recorded."""
        super().__init__(real)
        self.dup2_calls = 0

    def dup2(self, source: int, target: int) -> None:
        """Record the attempt and fail the way an unusable descriptor would."""
        self.dup2_calls += 1
        msg = "dup2 refused the descriptor"
        raise OSError(msg)


class _SelfDescriptorOs(_OsProxy):
    """An ``os`` proxy whose ``open`` reuses the stream's descriptor."""

    def __init__(self, real: types.ModuleType, descriptor: int) -> None:
        """Record the number the fake ``open`` should hand back."""
        super().__init__(real)
        self._descriptor = descriptor
        self.opened: list[tuple[str, int]] = []
        self.closed: list[int] = []

    def open(self, path: str, flags: int) -> int:
        """Record the open, redirect the reused number, and hand it back.

        The kernel can only return the stream's own number when that number is
        free, and opening the null device at a free number already points it at
        the null device. Model that outcome so the guard is tested against a
        real redirect rather than an unchanged descriptor.

        Returns
        -------
        int
            The stream's own descriptor number, now referring to the null
            device.
        """
        self.opened.append((path, flags))
        null_descriptor = self._real.open(path, flags)
        if null_descriptor == self._descriptor:
            return null_descriptor
        self._real.dup2(null_descriptor, self._descriptor)
        self._real.close(null_descriptor)
        return self._descriptor

    def close(self, descriptor: int) -> None:
        """Record each close so a test can prove one did not happen."""
        self.closed.append(descriptor)
        self._real.close(descriptor)


def assert_points_at_devnull(descriptor: int) -> None:
    """Fail unless *descriptor* now refers to the null device."""
    if not os.path.samestat(os.fstat(descriptor), Path(os.devnull).stat()):
        msg = f"descriptor {descriptor} should have been redirected to os.devnull"
        raise AssertionError(msg)
