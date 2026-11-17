"""Failure shapes and stand-in results for observe-hook dispatch tests.

These are the hook shapes whose *results* or *failures* sit at the edges of the
dispatcher's contract: plain values, non-coroutine awaitables, mocks, and every
kind of raised exception. They live apart from the ordinary hook-shape factories
so each module stays within the file-size limit, and because a failure shape is
interesting for what it throws rather than what it returns.
"""

from __future__ import annotations

import asyncio
import functools
import types
import typing as typ
from unittest import mock

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


class _HookError(Exception):
    """Raised by the ordinary-failure hook shape."""


class _FatalHookError(BaseException):
    """A non-``Exception`` failure, pinning the ``BaseException`` branch."""


class _CheckError(Exception):
    """Raised by a result whose ``__class__`` lookup fails."""


class _RaisingClass:
    """A result whose ``__class__`` property raises during ``isinstance``."""

    @property
    def __class__(self) -> type:
        """Raise so ``inspect.isawaitable`` propagates the failure."""
        raise _CheckError


class _AwaitableResult:
    """A non-coroutine awaitable, reached only by ``inspect.isawaitable``."""

    def __await__(self) -> cabc.Generator[None, None, None]:
        """Yield once so awaiting the result completes immediately."""
        yield


@types.coroutine
def _yield_once() -> cabc.Generator[None, None, None]:
    """Return a generator-based coroutine, the second ``isawaitable`` branch."""
    yield


class _AsyncMethod:
    """An instance whose bound method is an ``async def``."""

    def __init__(self, calls: list[ExecEvent]) -> None:
        """Bind the shared call recorder."""
        self.calls = calls

    async def observe(self, event: ExecEvent) -> None:
        """Record the event from a bound async method."""
        self.calls.append(event)


class _AsyncCallable:
    """An instance whose ``__call__`` is an ``async def``."""

    def __init__(self, calls: list[ExecEvent]) -> None:
        """Bind the shared call recorder."""
        self.calls = calls

    async def __call__(self, event: ExecEvent) -> None:
        """Record the event from an async ``__call__``."""
        self.calls.append(event)


class _SyncCallable:
    """An instance whose ``__call__`` is synchronous."""

    def __init__(self, calls: list[ExecEvent]) -> None:
        """Bind the shared call recorder."""
        self.calls = calls

    def __call__(self, event: ExecEvent) -> None:
        """Record the event from a synchronous ``__call__``."""
        self.calls.append(event)


class _AwaitFailureError(_HookError):
    """A scheduled result that fails while its task awaits it."""


def _failing_awaitable(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return an ``async def`` hook that records its event, then raises."""

    async def hook(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event, then fail the task that awaits this coroutine."""
        calls.append(event)
        raise _AwaitFailureError

    return hook


def _completed_future() -> asyncio.Future[None]:
    """Return a future that is already done, so awaiting it ends at once."""
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


def _recording_hook(
    calls: list[ExecEvent],
    _raised: list[BaseException],
    *,
    produce: cabc.Callable[[], object],
) -> ExecHook:
    """Return a hook that records its event and returns ``produce()``.

    The stand-in result may sit outside the declared contract — a plain value
    or a non-coroutine awaitable — because tolerating those shapes is what the
    dispatcher must do.

    Returns
    -------
    ExecHook
        A hook that records the event it receives and returns the produced
        stand-in result.
    """

    def hook(event: ExecEvent) -> object:
        """Record the event and return the stand-in result."""
        calls.append(event)
        return produce()

    return typ.cast("ExecHook", hook)


def _raising_hook(
    calls: list[ExecEvent],
    raised: list[BaseException],
    *,
    make_error: cabc.Callable[[], BaseException],
) -> ExecHook:
    """Return a hook that records its event, then raises ``make_error()``."""

    def hook(event: ExecEvent) -> None:
        """Record the event, then raise the stand-in error."""
        calls.append(event)
        error = make_error()
        raised.append(error)
        raise error

    return hook


type _Factory = cabc.Callable[
    [list[ExecEvent], list[BaseException]],
    ExecHook,
]

# Shapes whose result or failure sits at the edge of the contract. Merged with
# the ordinary shapes in ``_observe_hook_dispatch_support`` to form the domain.
EDGE_FACTORIES: dict[str, _Factory] = {
    "future": functools.partial(_recording_hook, produce=_completed_future),
    "generator_coroutine": functools.partial(_recording_hook, produce=_yield_once),
    "awaitable_object": functools.partial(_recording_hook, produce=_AwaitableResult),
    "mock_result": functools.partial(_recording_hook, produce=mock.Mock),
    "magic_mock_result": functools.partial(_recording_hook, produce=mock.MagicMock),
    "raising": functools.partial(
        _raising_hook,
        make_error=functools.partial(_HookError, "hook failed"),
    ),
    "raising_fatal": functools.partial(
        _raising_hook,
        make_error=functools.partial(_FatalHookError, "fatal hook failure"),
    ),
    "raising_cancelled": functools.partial(
        _raising_hook,
        make_error=asyncio.CancelledError,
    ),
    "check_raises": functools.partial(_recording_hook, produce=_RaisingClass),
    "failing_awaitable": _failing_awaitable,
}

# The shapes whose scheduled task raises when its result is awaited, with the
# exception the task must report. The task logs ``observe_hook_task_failed`` and
# re-raises, so the dispatcher sees the failure through the task rather than
# through hook emission.
TASK_FAILURE_KINDS: dict[str, type[BaseException]] = {
    "failing_awaitable": _AwaitFailureError,
}

__all__ = [
    "EDGE_FACTORIES",
    "TASK_FAILURE_KINDS",
    "_AsyncCallable",
    "_AsyncMethod",
    "_SyncCallable",
]
