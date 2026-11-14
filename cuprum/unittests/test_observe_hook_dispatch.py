"""Named examples for the result-guarded observe-hook dispatcher.

Roadmap 5.2.2 asks that known-sync hooks dispatch without per-line awaitable
detection while every awaitable shape keeps being scheduled. These examples
observe the dispatcher directly: a spy counts ``inspect.isawaitable`` calls
made *from* ``_emit_observe_hook`` only, so asyncio or pytest plugin traffic
cannot inflate or mask the count.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import gc
import inspect
import logging
import sys
import typing as typ
import warnings

import pytest

from cuprum import ECHO
from cuprum._observability import (
    _LOGGER,
    _emit_exec_event,
    _emit_observe_hook,
    _ExecEventEmissionError,
)
from cuprum.events import ExecEvent

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecHook, ExecPhase


class _ClassLookupError(RuntimeError):
    """Raised by an opaque failure's ``__class__`` property."""


class _OpaqueFailureError(Exception):
    """A hook failure that no ``isinstance`` check can classify."""

    @property
    def __class__(self) -> type:
        """Raise so a classifier that consults ``__class__`` cannot proceed."""
        raise _ClassLookupError


def _event(phase: str = "plan") -> ExecEvent:
    """Return a minimal event for ``phase``."""
    return ExecEvent(
        phase=typ.cast("ExecPhase", phase),
        program=ECHO,
        argv=(str(ECHO),),
        cwd=None,
        env=None,
        pid=None,
        timestamp=0.0,
        line=None,
        exit_code=None,
        duration_s=None,
        tags={},
    )


def _install_spy(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Patch ``inspect.isawaitable`` and return the results it was asked about.

    Only calls whose immediate caller is ``_emit_observe_hook`` are recorded, so
    the count is attributable to the dispatcher under test.

    Returns
    -------
    list[object]
        The results the dispatcher asked about, in call order.
    """
    seen: list[object] = []
    real = inspect.isawaitable

    def spy(obj: object) -> bool:
        """Record the argument when the dispatcher is the caller."""
        if sys._getframe(1).f_code is _emit_observe_hook.__code__:
            seen.append(obj)
        return real(obj)

    monkeypatch.setattr(inspect, "isawaitable", spy)
    return seen


def test_none_results_never_reach_isawaitable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A synchronous hook's ``None`` result is dismissed without detection."""
    seen = _install_spy(monkeypatch)

    def hook(_event: ExecEvent) -> None:
        """Return the only result a synchronous hook ever returns."""

    for _ in range(3):
        _emit_exec_event((hook,), _event())

    assert not seen, "a None result must never reach inspect.isawaitable"


def test_native_coroutines_never_reach_isawaitable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native coroutine is recognized by exact type, not by detection."""
    seen = _install_spy(monkeypatch)

    async def hook(_event: ExecEvent) -> None:
        """Return the only result an ``async def`` hook ever returns."""

    async def run() -> None:
        """Emit several events and settle every scheduled task."""
        for _ in range(3):
            scheduled = _emit_exec_event((hook,), _event())
            await asyncio.gather(*scheduled)

    asyncio.run(run())

    assert not seen, "a native coroutine must never reach inspect.isawaitable"


def test_awaitable_objects_still_reach_isawaitable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The spy sees the real call site for a non-native awaitable."""
    seen = _install_spy(monkeypatch)

    def hook(_event: ExecEvent) -> asyncio.Future[None]:
        """Return a ``Future``, which only the general test recognizes."""
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future

    async def run() -> None:
        """Emit one event and settle the scheduled task."""
        scheduled = _emit_exec_event((hook,), _event())
        await asyncio.gather(*scheduled)

    asyncio.run(run())

    assert len(seen) == 1, "a Future result must reach inspect.isawaitable exactly once"


async def _record(  # ruff: ignore[unused-async]
    calls: list[ExecEvent],
    event: ExecEvent,
) -> None:
    """Record one delivered event from an async body."""
    calls.append(event)


def _coroutine_returning_hook(
    shape: str,
    body: cabc.Callable[[ExecEvent], cabc.Coroutine[None, None, None]],
) -> ExecHook:
    """Build a synchronous callable of ``shape`` that returns a coroutine."""
    if shape == "plain":

        def hook(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
            """Return the coroutine the body builds for the event."""
            return body(event)

        return hook
    if shape == "lambda":
        # The lambda *is* the shape under test: a sync callable that returns a
        # coroutine without being a coroutine function.
        return lambda event: body(event)  # ruff: ignore[unnecessary-lambda]

    @functools.wraps(body)
    def wrapper(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
        """Return the wrapped body's coroutine for the event."""
        return body(event)

    return wrapper


@pytest.mark.parametrize("shape", ["plain", "lambda", "wraps"])
def test_sync_callable_returning_coroutine_is_still_scheduled(shape: str) -> None:
    """A sync callable's coroutine is scheduled exactly as an ``async def`` is.

    Classification by hook identity cannot see this shape: a plain function, a
    lambda, and a ``functools.wraps`` wrapper all report as synchronous while
    returning a coroutine. The dispatcher must therefore decide on the result.
    """
    completed: list[ExecEvent] = []

    def body(event: ExecEvent) -> cabc.Coroutine[None, None, None]:
        """Return the coroutine that records the event."""
        return _record(completed, event)

    hook = _coroutine_returning_hook(shape, body)

    async def run() -> None:
        """Emit one event, name the task, and settle it."""
        scheduled = _emit_exec_event((hook,), _event())
        assert len(scheduled) == 1, "the returned coroutine must be scheduled"
        assert scheduled[0].get_name() == "cuprum.observe.plan", (
            "the scheduled task must keep the observe task name"
        )
        await asyncio.gather(*scheduled)

    asyncio.run(run())

    assert len(completed) == 1, "the scheduled coroutine must run to completion"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        gc.collect()
    never_awaited = [
        warning for warning in caught if "never awaited" in str(warning.message)
    ]
    assert not never_awaited, "no coroutine may be left unawaited"


class _RecordSink(logging.Handler):
    """Collect records without formatting them."""

    def __init__(self) -> None:
        """Start with an empty record list."""
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Keep the record as emitted; formatting is deliberately not attempted."""
        self.records.append(record)


@contextlib.contextmanager
def _captured_records() -> cabc.Iterator[list[logging.LogRecord]]:
    """Capture cuprum's records without formatting an unformattable error."""
    sink = _RecordSink()
    previous_propagate = _LOGGER.propagate
    _LOGGER.addHandler(sink)
    _LOGGER.propagate = False
    try:
        yield sink.records
    finally:
        _LOGGER.removeHandler(sink)
        _LOGGER.propagate = previous_propagate


def _failure_records(
    records: cabc.Iterable[logging.LogRecord],
) -> list[logging.LogRecord]:
    """Return the ``observe_hook_failed`` records among ``records``."""
    return [
        record
        for record in records
        if record.getMessage().startswith("observe_hook_failed")
    ]


def test_opaque_failure_is_wrapped_and_keeps_the_scheduled_prefix() -> None:
    """A failure whose ``__class__`` raises is wrapped and logged, not lost.

    The base implementation matched cancellation with an ``except`` clause,
    which compares types without consulting ``__class__``. Classifying the
    caught error with ``isinstance`` would re-raise from inside the handler,
    replacing the original failure and dropping the tasks scheduled before it.
    """
    error = _OpaqueFailureError("opaque failure")
    recorded: list[ExecEvent] = []

    async def earlier(event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record the event once its task runs."""
        recorded.append(event)

    def failing(_event: ExecEvent) -> None:
        """Raise the error that no classifier can place."""
        raise error

    async def run() -> list[logging.LogRecord]:
        """Emit one event and settle whatever task was scheduled."""
        with _captured_records() as records:
            with pytest.raises(_ExecEventEmissionError) as exc_info:
                _emit_exec_event((earlier, failing), _event())
            emission = exc_info.value
            assert emission.error is error, "the original error must be wrapped"
            assert len(emission.scheduled_tasks) == 1, (
                "the scheduled prefix must survive the failure"
            )
            await asyncio.gather(*emission.scheduled_tasks, return_exceptions=True)
        return records

    records = asyncio.run(run())

    assert len(recorded) == 1, "the prefix task must still run"
    failures = _failure_records(records)
    assert len(failures) == 1, "the failure must still be logged"
    assert failures[0].cuprum_error_type == "_OpaqueFailureError", (
        "the record must name the failure's real type"
    )


def test_opaque_settled_failure_keeps_every_scheduled_task() -> None:
    """A settled failure retains tasks from hooks on both sides of the failure."""
    error = _OpaqueFailureError("opaque failure")
    recorded: list[str] = []

    async def earlier(_event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record that the hook before the failure ran."""
        recorded.append("earlier")

    def failing(_event: ExecEvent) -> None:
        """Raise the error that no classifier can place."""
        raise error

    async def later(_event: ExecEvent) -> None:  # ruff: ignore[unused-async]
        """Record that the hook after the failure still ran."""
        recorded.append("later")

    async def run() -> list[logging.LogRecord]:
        """Emit the terminal event and settle every retained task."""
        with _captured_records() as records:
            with pytest.raises(_ExecEventEmissionError) as exc_info:
                _emit_exec_event((earlier, failing, later), _event("settled"))
            emission = exc_info.value
            assert emission.error is error, "settlement must surface the first error"
            assert len(emission.scheduled_tasks) == 2, (
                "settlement must retain tasks scheduled after the failure too"
            )
            await asyncio.gather(*emission.scheduled_tasks, return_exceptions=True)
        return records

    records = asyncio.run(run())

    assert recorded == ["earlier", "later"], (
        "a settled failure must not stop later hooks"
    )
    assert len(_failure_records(records)) == 1, "the failure must still be logged"
