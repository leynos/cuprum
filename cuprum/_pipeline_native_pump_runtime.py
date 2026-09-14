"""Injectable executor and retention ownership for native pipeline pumps."""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses as dc
import functools
import threading
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


def _settle_native_pump_future(
    future: cf.Future[int],
    function: cabc.Callable[[int, int], int],
    reader_fd: int,
    writer_fd: int,
) -> None:
    """Run a native worker and publish its terminal state."""
    try:
        future.set_result(function(reader_fd, writer_fd))
    except BaseException as error:  # ruff: ignore[blind-except] - worker failures must settle the future.
        future.set_exception(error)


class _PersistentNativePumpExecutor(_NativePumpExecutor):
    """Run uninterruptible native I/O without interpreter-shutdown joining."""

    @typ.override
    def submit(
        self,
        function: cabc.Callable[[int, int], int],
        reader_fd: int,
        writer_fd: int,
        /,
    ) -> cf.Future[int]:
        """Submit one native reader/writer descriptor pair."""
        future: cf.Future[int] = cf.Future()
        worker = threading.Thread(
            target=functools.partial(
                _settle_native_pump_future,
                future,
                function,
                reader_fd,
                writer_fd,
            ),
            name="cuprum-native-pump",
            daemon=True,
        )
        worker.start()
        return future


@dc.dataclass(slots=True)
class _NativePumpRuntime:
    """Own native-worker execution and retention at one pump boundary."""

    executor: _NativePumpExecutor
    retained_futures: set[cf.Future[int]]


# Keep uninterruptible native I/O outside ``asyncio.run`` executor shutdown.
_DEFAULT_NATIVE_PUMP_RUNTIME = _NativePumpRuntime(
    executor=_PersistentNativePumpExecutor(),
    retained_futures=set(),
)
