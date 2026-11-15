"""Seeded-fault tests that prove the dispatch property can fail.

A property test that never rejects anything proves nothing. These tests run the
same comparison as ``test_observe_hook_dispatch_properties`` against deliberately
wrong dispatchers and against wrong evidence, each of which is one plausible
misreading of the contract: scheduling every non-``None`` result, scheduling
only native coroutines, calling the hooks in the wrong order, and reporting a
task failure without its exception. The comparison must report a mismatch for
each.
"""

from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import logging
import types
import typing as typ

from cuprum._observability import _await_awaitable, _emit_exec_event
from cuprum.unittests._observe_hook_dispatch_compare import make_event, mismatch
from cuprum.unittests._observe_hook_dispatch_oracle import _close_result

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from cuprum.events import ExecEvent, ExecHook


def _schedule_when(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
    *,
    should_schedule: cabc.Callable[[object], bool],
) -> list[asyncio.Task[None]]:
    """Dispatch under a caller-supplied predicate, for seeded-fault tests."""
    scheduled: list[asyncio.Task[None]] = []
    for hook in hooks:
        result = hook(event)
        if not should_schedule(result):
            _close_result(result)
            continue
        scheduled.append(
            asyncio.create_task(
                _await_awaitable(typ.cast("cabc.Awaitable[None]", result), event.phase),
                name=f"cuprum.observe.{event.phase}",
            )
        )
    return scheduled


def _is_not_none(result: object) -> bool:
    """Accept every non-``None`` result: one seeded fault."""
    return result is not None


def _is_native_coroutine(result: object) -> bool:
    """Accept only a native coroutine: the other seeded fault."""
    return type(result) is types.CoroutineType


def _schedule_reversed(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Schedule every awaitable result, but in reverse hook order."""
    produced = [hook(event) for hook in hooks]
    scheduled: list[asyncio.Task[None]] = []
    for result in reversed(produced):
        if not inspect.isawaitable(result):
            _close_result(result)
            continue
        scheduled.append(
            asyncio.create_task(
                _await_awaitable(result, event.phase),
                name=f"cuprum.observe.{event.phase}",
            )
        )
    return scheduled


def _call_reversed(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Call every hook in reverse order, but schedule in the declared order."""
    produced: list[object] = [hook(event) for hook in reversed(hooks)]
    scheduled: list[asyncio.Task[None]] = []
    for result in reversed(produced):
        if not inspect.isawaitable(result):
            _close_result(result)
            continue
        awaitable = typ.cast("cabc.Awaitable[None]", result)
        scheduled.append(
            asyncio.create_task(
                _await_awaitable(awaitable, event.phase),
                name=f"cuprum.observe.{event.phase}",
            )
        )
    return scheduled


def test_specification_rejects_reversed_call_order() -> None:
    """The property rejects a dispatcher that calls the hooks out of order."""

    async def run() -> None:
        """Observe the mismatch a reversed call order must produce."""
        found = await mismatch(
            _call_reversed,
            ["sync_none", "async_def"],
            make_event("plan"),
            (),
        )
        assert found is not None, "a reversed call order must be rejected"
        assert "hooks ran as" in found, (
            f"the mismatch must name the call order, got {found!r}"
        )

    asyncio.run(run())


def _without_exc_info(
    records: cabc.Iterable[logging.LogRecord],
) -> cabc.Iterator[logging.LogRecord]:
    """Yield each record with its exception detached, read when consumed.

    The comparison reads the records only after the dispatch it runs has
    logged, so a generator keeps this view of a live capture list in step with
    it instead of snapshotting an empty list.

    Yields
    ------
    logging.LogRecord
        Each captured record, with a copy whose ``exc_info`` is cleared in
        place of any record that carried one.
    """
    for record in records:
        if record.exc_info is None:
            yield record
            continue
        detached = copy.copy(record)
        detached.exc_info = None
        yield detached


def test_specification_rejects_a_failure_record_without_exc_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The property rejects a task failure reported without its exception."""
    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="cuprum._observability")

    async def run() -> None:
        """Observe the mismatch a stripped failure record must produce."""
        found = await mismatch(
            _emit_exec_event,
            ["failing_awaitable"],
            make_event("plan"),
            _without_exc_info(caplog.records),
        )
        assert found is not None, "a failure record without exc_info must be rejected"
        assert "in exc_info" in found, (
            f"the mismatch must name the missing exception, got {found!r}"
        )

    asyncio.run(run())


def test_specification_rejects_reversed_task_order() -> None:
    """The property rejects tasks that do not wrap their own hook's result."""

    async def run() -> None:
        """Observe the mismatch a reversed dispatcher must produce."""
        found = await mismatch(
            _schedule_reversed,
            ["async_def", "future"],
            make_event("plan"),
            (),
        )
        assert found is not None, "a reversed task order must be rejected"
        assert "not the result of the hook that scheduled it" in found, (
            f"the mismatch must name the task order, got {found!r}"
        )

    asyncio.run(run())


def test_specification_rejects_every_non_none_scheduled() -> None:
    """The property rejects a dispatcher that schedules a non-awaitable value."""

    async def run() -> None:
        """Observe the mismatch the wrong dispatcher must produce."""
        found = await mismatch(
            functools.partial(_schedule_when, should_schedule=_is_not_none),
            ["sync_value"],
            make_event("plan"),
            (),
        )
        assert found is not None, "a scheduled non-awaitable must be rejected"

    asyncio.run(run())


def test_specification_rejects_native_coroutines_only() -> None:
    """The property rejects a dispatcher that drops non-native awaitables."""

    async def run() -> None:
        """Observe a mismatch for each awaitable the wrong dispatcher drops."""
        for kind in ("future", "generator_coroutine", "awaitable_object"):
            found = await mismatch(
                functools.partial(
                    _schedule_when,
                    should_schedule=_is_native_coroutine,
                ),
                [kind],
                make_event("plan"),
                (),
            )
            assert found is not None, f"{kind} must be rejected"

    asyncio.run(run())
