"""Injectable executor and retention ownership for native pipeline pumps."""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses as dc
import functools
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class _NativePumpExecutor(typ.Protocol):
    """Executor capability required to start one native pump worker."""

    def submit(
        self,
        function: cabc.Callable[[int, int], int],
        reader_fd: int,
        writer_fd: int,
        /,
    ) -> cf.Future[int]:
        """Submit native pumping and return its terminal future."""
        raise NotImplementedError


class _ThreadPoolNativePumpExecutor:
    """Adapt the dedicated thread pool to the fixed native-pump call shape."""

    def __init__(self) -> None:
        """Create the executor that must outlive caller event loops."""
        self._executor = cf.ThreadPoolExecutor(thread_name_prefix="cuprum-native-pump")

    def submit(
        self,
        function: cabc.Callable[[int, int], int],
        reader_fd: int,
        writer_fd: int,
        /,
    ) -> cf.Future[int]:
        """Submit one native reader/writer descriptor pair."""
        worker = functools.partial(function, reader_fd, writer_fd)
        return self._executor.submit(worker)


@dc.dataclass(slots=True)
class _NativePumpRuntime:
    """Own native-worker execution and retention at one pump boundary."""

    executor: _NativePumpExecutor
    retained_futures: set[cf.Future[int]]


# Keep uninterruptible native I/O outside ``asyncio.run`` executor shutdown.
_DEFAULT_NATIVE_PUMP_RUNTIME = _NativePumpRuntime(
    executor=_ThreadPoolNativePumpExecutor(),
    retained_futures=set(),
)
