"""Hook factories and a specification oracle for observe-hook dispatch tests.

``_emit_exec_event`` decides whether to schedule a hook's result by inspecting
the *result*, not the hook. These helpers build one hook of every shape the
public contract supports, and an oracle that states what the dispatcher must do
for any sequence of those shapes. The property test compares the two; the
seeded-fault tests prove the oracle can reject a wrong dispatcher.

The oracle calls a *fresh* hook of each shape and asks
:func:`inspect.isawaitable` about the result it produced. That keeps the
expectation independent of the dispatcher's own decision, so a change that
narrowed the contract would fail the comparison rather than silently redefine
it.

Every ``async def`` here exists to *be* a coroutine function: that is the hook
shape under test, and several deliberately await nothing. Each therefore
carries a scoped ``ruff: ignore[unused-async]`` rather than an artificial
``await`` that would change the scheduling behaviour being pinned.
"""

from __future__ import annotations

import asyncio
import dataclasses as dc
import functools
import inspect
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


async def _record(  # ruff: ignore[unused-async]
    calls: list[ExecEvent],
    event: ExecEvent,
) -> None:
    """Record one delivered event from an async hook body."""
    calls.append(event)


class _AsyncMethod:
    """An instance whose bound method is an ``async def``."""

    async def observe(self, event: ExecEvent) -> None:
        """Record the event from a bound async method."""
        self.calls.append(event)

    def __init__(self, calls: list[ExecEvent]) -> None:
        """Bind the shared call recorder."""
        self.calls = calls


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


def _sync_none(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return a synchronous hook whose result is ``None``."""

    def hook(event: ExecEvent) -> None:
        """Record the event and return ``None``."""
        calls.append(event)

    return hook


def _sync_value(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return a synchronous hook whose result is a non-awaitable value."""

    def hook(event: ExecEvent) -> int:
        """Record the event and return a non-awaitable value."""
        calls.append(event)
        return 42

    # The public contract types a hook's result as awaitable-or-None, but the
    # dispatcher must also tolerate a plain value; that is the shape under test.
    return typ.cast("ExecHook", hook)


def _plain_coroutine(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a plain function that returns a coroutine."""

    def hook(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
        """Return the coroutine that records the event."""
        return _record(calls, event)

    return hook


def _lambda_coroutine(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a lambda that returns a coroutine."""
    return lambda event: _record(calls, event)


def _wraps_coroutine(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a ``functools.wraps`` sync wrapper of an ``async def``."""

    async def inner(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event from the wrapped coroutine."""
        calls.append(event)

    @functools.wraps(inner)
    def hook(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
        """Return the wrapped coroutine for the event."""
        return inner(event)

    return hook


def _async_def(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return an ``async def`` hook."""

    async def hook(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event from the coroutine function."""
        calls.append(event)

    return hook


def _partial_async(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a ``functools.partial`` of an ``async def``."""

    async def inner(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event from the partially applied coroutine."""
        calls.append(event)

    return functools.partial(inner)


def _bound_async_method(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a bound async method."""
    return _AsyncMethod(calls).observe


def _async_call_instance(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return an instance with an ``async def __call__``."""
    return _AsyncCallable(calls)


def _sync_call_instance(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return an instance with a synchronous ``__call__``."""
    return _SyncCallable(calls)


def _async_mock(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return an ``AsyncMock`` whose call returns a coroutine."""

    async def side_effect(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event from the mock's coroutine side effect."""
        calls.append(event)

    return mock.AsyncMock(side_effect=side_effect)


def _autospec_async(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a ``create_autospec`` double of an ``async def``."""

    async def template(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event from the autospec template."""
        calls.append(event)

    def side_effect(event: ExecEvent) -> None:
        """Record the event from the autospec double's side effect."""
        calls.append(event)

    hook = mock.create_autospec(template)
    hook.side_effect = side_effect
    return hook


def _marked_coroutine_function(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a ``markcoroutinefunction``-marked function returning ``None``."""

    def hook(event: ExecEvent) -> None:
        """Record the event and return ``None`` despite the marker."""
        calls.append(event)

    return inspect.markcoroutinefunction(hook)


def _future(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return a hook whose result is a completed ``asyncio.Future``."""

    def hook(event: ExecEvent) -> asyncio.Future[None]:
        """Record the event and return a completed future."""
        calls.append(event)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    return hook


def _generator_coroutine(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a hook whose result is a ``@types.coroutine`` generator."""

    def hook(event: ExecEvent) -> cabc.Awaitable[None]:
        """Record the event and return a generator-based awaitable."""
        calls.append(event)
        return _yield_once()

    return hook


def _awaitable_object(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a hook whose result defines ``__await__``."""

    def hook(event: ExecEvent) -> _AwaitableResult:
        """Record the event and return an ``__await__`` object."""
        calls.append(event)
        return _AwaitableResult()

    return hook


def _mock_result(calls: list[ExecEvent], _raised: list[BaseException]) -> ExecHook:
    """Return a hook whose result is a plain ``Mock``."""

    def side_effect(event: ExecEvent) -> mock.Mock:
        """Record the event and return a plain mock."""
        calls.append(event)
        return mock.Mock()

    return mock.Mock(side_effect=side_effect)


def _magic_mock_result(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a hook whose result is a ``MagicMock``."""

    def side_effect(event: ExecEvent) -> mock.MagicMock:
        """Record the event and return a magic mock."""
        calls.append(event)
        return mock.MagicMock()

    return mock.MagicMock(side_effect=side_effect)


def _raising(
    calls: list[ExecEvent],
    raised: list[BaseException],
) -> ExecHook:
    """Return a hook that raises an ordinary ``Exception``."""

    def hook(event: ExecEvent) -> None:
        """Record the event, then raise an ordinary exception."""
        calls.append(event)
        error = _HookError("hook failed")
        raised.append(error)
        raise error

    return hook


def _raising_fatal(
    calls: list[ExecEvent],
    raised: list[BaseException],
) -> ExecHook:
    """Return a hook that raises a non-``Exception`` ``BaseException``."""

    def hook(event: ExecEvent) -> None:
        """Record the event, then raise a non-``Exception`` error."""
        calls.append(event)
        error = _FatalHookError("fatal hook failure")
        raised.append(error)
        raise error

    return hook


def _raising_cancelled(
    calls: list[ExecEvent],
    raised: list[BaseException],
) -> ExecHook:
    """Return a hook that raises ``CancelledError``."""

    def hook(event: ExecEvent) -> None:
        """Record the event, then raise ``CancelledError``."""
        calls.append(event)
        error = asyncio.CancelledError()
        raised.append(error)
        raise error

    return hook


def _check_raises(
    calls: list[ExecEvent],
    _raised: list[BaseException],
) -> ExecHook:
    """Return a hook whose result makes ``inspect.isawaitable`` raise."""

    def hook(event: ExecEvent) -> _RaisingClass:
        """Record the event and return a result that breaks the check."""
        calls.append(event)
        return _RaisingClass()

    # The result is deliberately outside the declared contract: the point is
    # that the awaitable check itself raises on it.
    return typ.cast("ExecHook", hook)


type _Factory = cabc.Callable[
    [list[ExecEvent], list[BaseException]],
    ExecHook,
]

# Every shape the public observe-hook contract supports, plus the failure and
# edge shapes the dispatcher must keep handling identically. The names are the
# Hypothesis domain and the ``hypothesis.event`` tags.
_FACTORIES: dict[str, _Factory] = {
    "sync_none": _sync_none,
    "sync_value": _sync_value,
    "plain_coroutine": _plain_coroutine,
    "lambda_coroutine": _lambda_coroutine,
    "wraps_coroutine": _wraps_coroutine,
    "async_def": _async_def,
    "partial_async": _partial_async,
    "bound_async_method": _bound_async_method,
    "async_call_instance": _async_call_instance,
    "sync_call_instance": _sync_call_instance,
    "async_mock": _async_mock,
    "autospec_async": _autospec_async,
    "marked_coroutine_function": _marked_coroutine_function,
    "future": _future,
    "generator_coroutine": _generator_coroutine,
    "awaitable_object": _awaitable_object,
    "mock_result": _mock_result,
    "magic_mock_result": _magic_mock_result,
    "raising": _raising,
    "raising_fatal": _raising_fatal,
    "raising_cancelled": _raising_cancelled,
    "check_raises": _check_raises,
}

HOOK_KINDS: tuple[str, ...] = tuple(_FACTORIES)


@dc.dataclass(slots=True)
class HookProbe:
    """One hook instance plus the observations the oracle needs.

    Attributes
    ----------
    kind:
        The factory name the hook was built from.
    hook:
        The callable handed to the dispatcher.
    calls:
        Events the hook received, in delivery order.
    raised:
        Exceptions the hook raised, so the test can prove the dispatcher
        wrapped the original object rather than a copy.

    """

    kind: str
    hook: ExecHook
    calls: list[ExecEvent] = dc.field(default_factory=list)
    raised: list[BaseException] = dc.field(default_factory=list)


def make_probe(kind: str) -> HookProbe:
    """Build one fresh hook of ``kind`` with its own call recorder."""
    calls: list[ExecEvent] = []
    raised: list[BaseException] = []
    return HookProbe(
        kind=kind,
        hook=_FACTORIES[kind](calls, raised),
        calls=calls,
        raised=raised,
    )


@dc.dataclass(frozen=True, slots=True)
class DispatchExpectation:
    """What the dispatcher must do for one sequence of hook kinds.

    Attributes
    ----------
    called:
        Kinds the dispatcher must call, in order, up to and including the
        first raising or check-raising hook.
    scheduled:
        Kinds whose result is awaitable, among the called non-raising hooks.
    error:
        The exception the first raising hook raised, or ``None``.
    check_error:
        The exception ``inspect.isawaitable`` raised, or ``None``. It escapes
        the dispatcher unwrapped, so it takes precedence over ``error``.

    """

    called: tuple[str, ...]
    scheduled: tuple[str, ...]
    error: BaseException | None
    check_error: BaseException | None


def _close_result(result: object) -> None:
    """Close a coroutine or generator the oracle produced but will not await.

    The exact-type test is deliberate: ``inspect.iscoroutine`` consults
    ``__class__``, which the ``check_raises`` shape makes raise, and closing a
    result must not mask the failure the oracle is about to report.
    """
    if type(result) in {types.CoroutineType, types.GeneratorType}:
        typ.cast("cabc.Coroutine[object, object, object]", result).close()


def expected_dispatch(
    kinds: cabc.Sequence[str],
    event: ExecEvent,
) -> DispatchExpectation:
    """State what the dispatcher must do for ``kinds`` and ``event``.

    The expectation is derived by calling a fresh hook of each kind and asking
    :func:`inspect.isawaitable` about the result, so it is independent of the
    dispatcher's own decision.

    Returns
    -------
    DispatchExpectation
        The hooks that must be called, the results that must be scheduled, and
        the failure the sequence must produce, if any.
    """
    called: list[str] = []
    scheduled: list[str] = []
    for kind in kinds:
        probe = make_probe(kind)
        called.append(kind)
        try:
            result = probe.hook(event)
        # Any failure is part of the specification, including the
        # non-``Exception`` shapes the dispatcher must still wrap.
        except BaseException as exc:  # ruff: ignore[blind-except]
            return DispatchExpectation(
                called=tuple(called),
                scheduled=tuple(scheduled),
                error=exc,
                check_error=None,
            )
        try:
            is_awaitable = inspect.isawaitable(result)
        # The ``check_raises`` shape makes the check itself raise; that escape
        # is part of the specification too.
        except BaseException as exc:  # ruff: ignore[blind-except]
            return DispatchExpectation(
                called=tuple(called),
                scheduled=tuple(scheduled),
                error=None,
                check_error=exc,
            )
        finally:
            _close_result(result)
        if is_awaitable:
            scheduled.append(kind)
    return DispatchExpectation(
        called=tuple(called),
        scheduled=tuple(scheduled),
        error=None,
        check_error=None,
    )
