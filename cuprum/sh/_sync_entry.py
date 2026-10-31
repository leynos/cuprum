"""Guarded execution for the synchronous command entry points.

``SafeCmd.run_sync``, ``SafeCmd.run_bytes_sync``, ``Pipeline.run_sync``, and
``Pipeline.run_bytes_sync`` all drive their asynchronous twin with
``asyncio.run``. That function refuses to start a loop while another loop is
running in the same thread, and the ``RuntimeError`` it raises neither names the
Cuprum method nor points at the asynchronous alternative. This module fronts
that refusal with a documented, typed error, and does so before the coroutine is
constructed, so a refused call cannot also emit a "coroutine was never awaited"
warning.
"""

from __future__ import annotations

import asyncio
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = ["RunningEventLoopError"]


class RunningEventLoopError(RuntimeError):
    """Raised when a synchronous entry point is called inside a running loop.

    The synchronous entry points own the event loop they create, so they cannot
    run when the calling thread already has a loop running. Await the
    asynchronous counterpart instead.

    The type subclasses :class:`RuntimeError` so callers that already catch the
    ``asyncio.run`` refusal keep working, while gaining an error they can
    distinguish from an unrelated ``RuntimeError``.

    Attributes
    ----------
    entry_point : str
        The synchronous method the caller invoked, for example
        ``"run_sync()"``.
    async_alternative : str
        The asynchronous method to await instead, for example ``"run()"``.
    """

    def __init__(self, *, entry_point: str, async_alternative: str) -> None:
        """Store the refusing entry point and the alternative to name."""
        super().__init__(
            f"{entry_point} cannot be called from a running event loop; "
            f"await {async_alternative} instead",
        )
        self.entry_point = entry_point
        self.async_alternative = async_alternative


def _run_sync[T](
    coroutine_factory: cabc.Callable[[], cabc.Coroutine[typ.Any, typ.Any, T]],
    *,
    entry_point: str,
    async_alternative: str,
) -> T:
    """Run a coroutine factory to completion unless a loop is already running.

    ``asyncio.run`` is called only when the current thread has no running loop.
    The factory is not called on the refusing path, so no coroutine is created
    only to be discarded.

    Parameters
    ----------
    coroutine_factory : collections.abc.Callable[
        [], collections.abc.Coroutine[Any, Any, T]
    ]
        Builds the coroutine to run. It is called at most once.
    entry_point : str
        The synchronous method name reported in the error.
    async_alternative : str
        The asynchronous method name reported in the error.

    Returns
    -------
    T
        The coroutine's result.

    Raises
    ------
    RunningEventLoopError
        If an event loop is already running in the calling thread.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine_factory())
    raise RunningEventLoopError(
        entry_point=entry_point,
        async_alternative=async_alternative,
    )
