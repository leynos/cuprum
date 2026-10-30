"""Chaining several per-line hooks into one fan-out hook.

``SafeCmd.lines()`` registers the caller's ``on_line`` alongside the driver's
queue sink, and both must see every line: the caller's hook runs first so its
view of the stream never depends on how the driver delivers. The chain built
here also keeps the backpressure contract intact — every hook's awaitable is
collected before any is awaited, and the fan-out returns one awaitable of its
own so the drain loop waits for each outcome before reading on.

Composition of the observe-hook emission with the caller's callback lives in
:mod:`cuprum._line_callbacks`; this module only combines hooks that have
already been composed.
"""

from __future__ import annotations

import functools
import inspect
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.lines import LineEvent, _LineHookFn, _LineHookOutcome


def _chain_line_hooks(
    first: _LineHookFn,
    *remaining: _LineHookFn,
) -> _LineHookFn:
    """Chain several line hooks into one, in the order supplied.

    ``SafeCmd.lines()`` registers the caller's ``on_line`` alongside the
    driver's queue sink, and both must see every line: the caller's hook runs
    first so its view of the stream never depends on how the driver delivers.
    Requiring the first hook keeps the result callable by construction.

    Returns
    -------
    _LineHookFn
        The sole hook when it is the only one, or a fan-out over all hooks.
        The fan-out collects every hook's awaitable and returns one awaitable
        of its own, so the drain loop still waits for each before reading on.
    """
    chain = (first, *remaining)
    if len(chain) == 1:
        return first
    return _fan_out_hooks(chain)


def _fan_out_hooks(chain: cabc.Sequence[_LineHookFn]) -> _LineHookFn:
    """Return one hook that delivers every event to each hook in *chain*."""
    return typ.cast("_LineHookFn", functools.partial(_fan_out_event, tuple(chain)))


def _fan_out_event(
    chain: cabc.Sequence[_LineHookFn],
    event: LineEvent,
) -> _LineHookOutcome:
    """Deliver one event to every hook before awaiting their outcomes."""
    pending = [outcome for hook in chain if (outcome := hook(event)) is not None]
    if not pending:
        return None
    return _await_hook_outcomes(pending)


async def _await_hook_outcomes(pending: cabc.Sequence[cabc.Awaitable[None]]) -> None:
    """Await every deferred hook, in registration order."""
    for index, outcome in enumerate(pending):
        try:
            await outcome
        except BaseException:
            _close_skipped_hook_outcomes(pending[index + 1 :])
            raise


def _close_skipped_hook_outcomes(pending: cabc.Iterable[cabc.Awaitable[None]]) -> None:
    """Close unawaited coroutines left after an earlier hook failure."""
    for outcome in pending:
        if inspect.iscoroutine(outcome):
            outcome.close()


__all__ = ["_chain_line_hooks", "_fan_out_hooks"]
