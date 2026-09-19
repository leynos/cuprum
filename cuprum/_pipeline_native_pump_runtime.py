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


# Idle workers are kept for reuse, because a repeated hand-off should not pay
# for a fresh thread every time. The count is only a *retention* limit: a
# hand-off that arrives while all retained workers are busy starts another
# worker instead of queueing behind one.
_IDLE_NATIVE_PUMP_WORKERS = 4


@dc.dataclass(frozen=True, slots=True)
class _NativePumpJob:
    """One native pump call awaiting a worker."""

    function: cabc.Callable[[int, int], int]
    reader_fd: int
    writer_fd: int
    future: cf.Future[int]


class _NativePumpWorker:
    """A daemon worker thread and the inbox only its next hand-off uses."""

    __slots__ = ("inbox", "thread")

    def __init__(self, run: cabc.Callable[["_NativePumpWorker"], None]) -> None:
        """Create an unstarted worker that runs ``run`` over itself."""
        self.inbox: queue.SimpleQueue[_NativePumpJob] = queue.SimpleQueue()
        self.thread = threading.Thread(
            target=run,
            args=(self,),
            name="cuprum-native-pump",
            daemon=True,
        )

    def start(self) -> None:
        """Start the worker thread."""
        self.thread.start()


class _PooledNativePumpExecutor(_NativePumpExecutor):
    """Run uninterruptible native I/O on disposable daemon workers.

    Each submission is handed straight to a worker thread: either one already
    idle, or a newly started one. Nothing is ever queued behind running work,
    and that is a correctness requirement rather than a tuning choice. A
    native pump blocks while its downstream pipe is full, and only a *later*
    hop in the same pipeline can drain that pipe, so a pump that waits for a
    running worker to finish can wait forever. A pipeline with more inter-stage
    hops than the pool has workers therefore deadlocks as soon as its payload
    exceeds the pipe capacity that the hops share — so the number of pumps
    that may run at once is unbounded by design, and `submit` never applies
    back-pressure. Bounding the queue, or waiting for a free worker, would
    reintroduce exactly that hang.

    What *is* bounded is idle retention: workers that finish a hand-off return
    to a small pool of idleness and are reused, so repeated hand-offs do not
    pay for a thread each time, while a burst of concurrent hops is served by
    starting the threads it needs and letting the surplus retire.

    The workers are daemon threads on purpose: ``concurrent.futures``
    executors register an ``atexit`` join that would block interpreter
    shutdown on a stuck native worker, which is the exact failure mode this
    executor exists to avoid.
    """

    def __init__(self, idle_limit: int = _IDLE_NATIVE_PUMP_WORKERS) -> None:
        """Retain up to ``idle_limit`` workers for reuse, starting none yet."""
        self._idle_limit = idle_limit
        self._idle: list[_NativePumpWorker] = []
        self._idle_lock = threading.Lock()

    def _start_worker(self) -> _NativePumpWorker:
        """Start one daemon worker with an inbox dedicated to its next job."""
        worker = _NativePumpWorker(self._worker_loop)
        worker.start()
        return worker

    def _take_worker(self) -> _NativePumpWorker:
        """Take an idle worker, or start one when every worker is busy."""
        with self._idle_lock:
            if self._idle:
                return self._idle.pop()
        return self._start_worker()

    def _retire_or_reuse(self, worker: _NativePumpWorker) -> bool:
        """Keep a finished worker for reuse, or report that it should exit."""
        with self._idle_lock:
            if len(self._idle) >= self._idle_limit:
                return False
            self._idle.append(worker)
            return True

    def _worker_loop(self, worker: _NativePumpWorker) -> None:
        """Settle hand-offs until the idle pool is full, then retire."""
        while True:
            job = worker.inbox.get()
            _settle_native_pump_future(
                job.future,
                job.function,
                job.reader_fd,
                job.writer_fd,
            )
            if not self._retire_or_reuse(worker):
                return

    @typ.override
    def submit(
        self,
        function: cabc.Callable[[int, int], int],
        reader_fd: int,
        writer_fd: int,
        /,
    ) -> cf.Future[int]:
        """Hand one native descriptor pair to a worker without queueing it."""
        future: cf.Future[int] = cf.Future()
        job = _NativePumpJob(
            function=function,
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            future=future,
        )
        self._take_worker().inbox.put(job)
        return future


@dc.dataclass(slots=True)
class _NativePumpRuntime:
    """Own native-worker execution and retention at one pump boundary."""

    executor: _NativePumpExecutor
    retained_futures: set[cf.Future[int]]


# Keep uninterruptible native I/O outside ``asyncio.run`` executor shutdown.
_DEFAULT_NATIVE_PUMP_RUNTIME = _NativePumpRuntime(
    executor=_PooledNativePumpExecutor(),
    retained_futures=set(),
)
