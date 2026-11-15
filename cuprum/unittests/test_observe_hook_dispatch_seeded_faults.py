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

type _Dispatch = cabc.Callable[
    [tuple[ExecHook, ...], ExecEvent],
    list[asyncio.Task[None]],
]


def _scheduled(
    results: cabc.Iterable[object],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Schedule one task per result, whatever its awaitability."""
    return [
        asyncio.create_task(
            _await_awaitable(typ.cast("cabc.Awaitable[None]", result), event.phase),
            name=f"cuprum.observe.{event.phase}",
        )
        for result in results
    ]


def _awaitable_results(results: cabc.Iterable[object]) -> list[object]:
    """Keep the awaitable results and close the rest."""
    kept: list[object] = []
    for result in results:
        if inspect.isawaitable(result):
            kept.append(result)
        else:
            _close_result(result)
    return kept


def _schedule_when(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
    *,
    should_schedule: cabc.Callable[[object], bool],
) -> list[asyncio.Task[None]]:
    """Dispatch under a caller-supplied predicate, for seeded-fault tests."""
    kept: list[object] = []
    for hook in hooks:
        result = hook(event)
        if should_schedule(result):
            kept.append(result)
        else:
            _close_result(result)
    return _scheduled(kept, event)


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
    produced = _awaitable_results(hook(event) for hook in hooks)
    return _scheduled(reversed(produced), event)


def _call_reversed(
    hooks: tuple[ExecHook, ...],
    event: ExecEvent,
) -> list[asyncio.Task[None]]:
    """Call every hook in reverse order, but schedule in the declared order."""
    produced = _awaitable_results(hook(event) for hook in reversed(hooks))
    return _scheduled(reversed(produced), event)


def _assert_rejected(
    dispatch: _Dispatch,
    kinds: cabc.Sequence[str],
    records: cabc.Iterable[logging.LogRecord] = (),
    *,
    mentions: str | None = None,
) -> None:
    """Require the comparison to reject ``dispatch``, optionally naming why."""
    found = asyncio.run(mismatch(dispatch, kinds, make_event("plan"), records))
    assert found is not None, f"{kinds} must be rejected"
    if mentions is not None:
        assert mentions in found, (
            f"the mismatch must mention {mentions!r}, got {found!r}"
        )


def test_specification_rejects_reversed_call_order() -> None:
    """The property rejects a dispatcher that calls the hooks out of order."""
    _assert_rejected(
        _call_reversed,
        ("sync_none", "async_def"),
        mentions="hooks ran as",
    )


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
    _assert_rejected(
        _emit_exec_event,
        ("failing_awaitable",),
        _without_exc_info(caplog.records),
        mentions="in exc_info",
    )


def test_specification_rejects_reversed_task_order() -> None:
    """The property rejects tasks that do not wrap their own hook's result."""
    _assert_rejected(
        _schedule_reversed,
        ("async_def", "future"),
        mentions="not the result of the hook that scheduled it",
    )


def test_specification_rejects_every_non_none_scheduled() -> None:
    """The property rejects a dispatcher that schedules a non-awaitable value."""
    _assert_rejected(
        functools.partial(_schedule_when, should_schedule=_is_not_none),
        ("sync_value",),
    )


def test_specification_rejects_native_coroutines_only() -> None:
    """The property rejects a dispatcher that drops non-native awaitables."""
    for kind in ("future", "generator_coroutine", "awaitable_object"):
        _assert_rejected(
            functools.partial(_schedule_when, should_schedule=_is_native_coroutine),
            (kind,),
        )
