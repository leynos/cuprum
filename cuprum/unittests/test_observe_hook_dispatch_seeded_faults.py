"""Seeded-fault tests that prove the dispatch property can fail.

A property test that never rejects anything proves nothing. These tests run the
same comparison as ``test_observe_hook_dispatch_properties`` against deliberately
wrong dispatchers, each of which is one of the two plausible misreadings of the
contract: scheduling every non-``None`` result, or scheduling only native
coroutines. The comparison must report a mismatch for each.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import types
import typing as typ

from cuprum._observability import _await_awaitable
from cuprum.unittests._observe_hook_dispatch_compare import make_event, mismatch
from cuprum.unittests._observe_hook_dispatch_oracle import _close_result

if typ.TYPE_CHECKING:
    import collections.abc as cabc

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
                _await_awaitable(typ.cast("cabc.Awaitable[None]", result), event.phase),
                name=f"cuprum.observe.{event.phase}",
            )
        )
    return scheduled


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
