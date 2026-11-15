"""Hook factories for observe-hook dispatch tests.

``_emit_exec_event`` decides whether to schedule a hook's result by inspecting
the *result*, not the hook. These helpers build one hook of every shape the
public contract supports, record what each produced, and expose that record to
the oracle in ``_observe_hook_dispatch_oracle``. The edge shapes — failures,
stand-in values, and mocks — live in ``_observe_hook_dispatch_shapes``.

Every ``async def`` here exists to *be* a coroutine function: that is the hook
shape under test, and several deliberately await nothing. Each therefore
carries a scoped ``ruff: ignore[unused-async]`` rather than an artificial
``await`` that would change the scheduling behaviour being pinned.
"""

from __future__ import annotations

import dataclasses as dc
import functools
import inspect
import typing as typ
from unittest import mock

from cuprum.unittests._observe_hook_dispatch_shapes import (
    EDGE_FACTORIES,
    _AsyncCallable,
    _AsyncMethod,
    _SyncCallable,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent, ExecHook


async def _record(  # ruff: ignore[unused-async]
    calls: list[ExecEvent],
    event: ExecEvent,
) -> None:
    """Record one delivered event from an async hook body."""
    calls.append(event)


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
    **EDGE_FACTORIES,
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
    results:
        Objects the hook returned, in call order, so the test can prove each
        scheduled task wraps the result of the hook that produced it.

    """

    kind: str
    hook: ExecHook
    calls: list[ExecEvent] = dc.field(default_factory=list)
    raised: list[BaseException] = dc.field(default_factory=list)
    results: list[object] = dc.field(default_factory=list)


def make_probe(kind: str, *, trace: list[str] | None = None) -> HookProbe:
    """Build one fresh hook of ``kind`` with its own recorders.

    Parameters
    ----------
    kind:
        The factory name of the hook shape to build.
    trace:
        A list shared by every probe of one sequence. Each hook that the
        dispatcher calls appends its kind, in call order, so the comparison can
        see the order hooks ran in and not merely how often each ran.

    Returns
    -------
    HookProbe
        The hook and the recorders it fills.
    """
    calls: list[ExecEvent] = []
    raised: list[BaseException] = []
    results: list[object] = []
    inner = _FACTORIES[kind](calls, raised)

    def hook(event: ExecEvent) -> object:
        """Record the call, then the produced result, and return that result."""
        if trace is not None:
            trace.append(kind)
        result = inner(event)
        results.append(result)
        return result

    return HookProbe(
        kind=kind,
        hook=typ.cast("ExecHook", hook),
        calls=calls,
        raised=raised,
        results=results,
    )
