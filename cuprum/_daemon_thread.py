"""Await bounded teardown calls without depending on the shared executor."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _set_result[T](future: asyncio.Future[T], result: T) -> None:
    """Set a daemon operation result unless its waiter has already gone."""
    if not future.done():
        future.set_result(result)


def _set_exception[T](
    future: asyncio.Future[T],
    error: BaseException,
) -> None:
    """Set a daemon operation error unless its waiter has already gone."""
    if not future.done():
        future.set_exception(error)


async def _await_daemon_thread[T](operation: cabc.Callable[[], T]) -> T:
    """Run one bounded teardown operation on an owned daemon thread."""
    loop = asyncio.get_running_loop()
    completion: asyncio.Future[T] = loop.create_future()
    context = contextvars.copy_context()

    def run() -> None:
        """Run the operation and return its outcome to the owning loop."""
        try:
            result = context.run(operation)
        except BaseException as error:  # ruff: ignore[blind-except] - propagate every thread outcome.
            loop.call_soon_threadsafe(_set_exception, completion, error)
        else:
            loop.call_soon_threadsafe(_set_result, completion, result)

    thread = threading.Thread(
        target=run,
        name="cuprum-bounded-output-cleanup",
        daemon=True,
    )
    thread.start()
    return await completion
