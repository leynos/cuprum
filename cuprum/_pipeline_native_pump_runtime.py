"""Injectable executor and retention ownership for native pipeline pumps."""

from __future__ import annotations

import concurrent.futures as cf
import dataclasses as dc
import queue
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


# The pool stays deliberately small: it only has to cover the native pumps of
# concurrently running pipelines, not general-purpose parallelism.
_PERSISTENT_NATIVE_PUMP_WORKERS = 4


class _PersistentNativePumpExecutor(_NativePumpExecutor):
    """Run uninterruptible native I/O without interpreter-shutdown joining."""

    def __init__(self, worker_count: int = _PERSISTENT_NATIVE_PUMP_WORKERS) -> None:
        """Start a fixed pool of daemon workers that outlive this executor.

        The workers are daemon threads on purpose: ``concurrent.futures``
        executors register an ``atexit`` join that would block interpreter
        shutdown on a stuck native worker, which is the exact failure mode
        this executor exists to avoid.
        """
        self._work: queue.SimpleQueue[
            tuple[cabc.Callable[[int, int], int], int, int, cf.Future[int]]
        ] = queue.SimpleQueue()
        self._workers = tuple(
            threading.Thread(
                target=self._worker_loop,
                name="cuprum-native-pump",
                daemon=True,
            )
            for _ in range(worker_count)
        )
        for worker in self._workers:
            worker.start()

    def _worker_loop(self) -> None:
        """Settle one submitted pump at a time, forever."""
        while True:
            function, reader_fd, writer_fd, future = self._work.get()
            _settle_native_pump_future(future, function, reader_fd, writer_fd)

    @typ.override
    def submit(
        self,
        function: cabc.Callable[[int, int], int],
        reader_fd: int,
        writer_fd: int,
        /,
    ) -> cf.Future[int]:
        """Submit one native reader/writer descriptor pair to the pool."""
        future: cf.Future[int] = cf.Future()
        self._work.put((function, reader_fd, writer_fd, future))
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
