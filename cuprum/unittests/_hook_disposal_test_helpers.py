"""Started-coroutine fixtures shared by hook-disposal regression suites."""

from __future__ import annotations

import types
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_EventT = typ.TypeVar("_EventT")


@types.coroutine
def _suspend() -> cabc.Generator[None, None, None]:
    """Yield once so the awaiting coroutine can be suspended."""
    yield


def _started_coroutine_raising(
    error: BaseException,
) -> cabc.Coroutine[object, object, None]:
    """Return a started coroutine whose ``finally`` raises ``error`` on close.

    Parameters
    ----------
    error:
        Failure raised when the coroutine is closed after it has started.

    Returns
    -------
    cabc.Coroutine[object, object, None]
        A coroutine suspended inside a ``try`` block.

    Notes
    -----
    ``send(None)`` enters the ``try`` block and suspends at ``_suspend``;
    closing the coroutine then executes its ``finally`` block.
    """

    async def body() -> None:
        """Suspend inside ``try`` so closing executes ``finally``."""
        try:
            await _suspend()
        finally:
            raise error

    coro = body()
    coro.send(None)
    return coro


def hook_returning(
    coro: cabc.Coroutine[object, object, None],
) -> cabc.Callable[[_EventT], None]:
    """Build a hook that returns ``coro`` despite its synchronous contract.

    Parameters
    ----------
    coro:
        Started coroutine returned by the hook.

    Returns
    -------
    cabc.Callable[[_EventT], None]
        Hook accepting any event type and returning the supplied coroutine.
    """

    def hook(_event: _EventT) -> None:
        """Return the coroutine the hook contract forbids."""
        return typ.cast("None", coro)

    return hook
