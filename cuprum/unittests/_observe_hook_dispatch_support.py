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
