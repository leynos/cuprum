"""A failing output callback must end the run, not block on a full pipe.

A raising ``on_line`` callback ends the task draining the child's output. The
child keeps writing, nothing reads its pipe, and the exit the coordinator is
waiting for never arrives — so a run with no ``timeout=`` would wait forever.
Both entry points must instead notice the failed consumer, terminate and reap
the child, settle their siblings, and re-raise the callback's own exception.

Each case is bounded by a watchdog of its own rather than by a configured
execution timeout: the point is that no deadline is needed for the run to end.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import typing as typ

import pytest

from cuprum import RunOutputOptions, sh
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.timeouts import (
    pending_tasks,
    process_is_running,
    started_pids,
    wait_for_process_death,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecEvent
    from cuprum.lines import LineEvent, LineHook
    from cuprum.sh import SafeCmd

# The watchdog is generous enough for a cold interpreter start and far shorter
# than the point at which a blocked child would look like ordinary slowness.
_WATCHDOG_S = 10.0
# The child never stops writing, so without the fail-fast path it blocks once
# the pipe fills and the run's exit wait never settles.
_CHILD_SCRIPT = (
    "import sys\nwhile True:\n    print('endless', file=sys.{stream}, flush=True)\n"
)


class _CallbackError(RuntimeError):
    """The exception a failing output callback raises."""


def _sync_callback(
    sentinel: BaseException,
) -> cabc.Callable[[LineEvent], None]:
    """Return a callback that raises *sentinel* synchronously."""

    def fail(_event: LineEvent) -> None:
        """Raise *sentinel* at the point the callback is invoked."""
        raise sentinel

    return fail


def _async_callback(
    sentinel: BaseException,
) -> cabc.Callable[[LineEvent], cabc.Awaitable[None]]:
    """Return a callback that raises *sentinel* when its result is awaited."""

    # RUF029: the callback contract accepts a coroutine function, and the point
    # of this case is that a failure raised *inside* it is still reported; the
    # raise happens before any await, so there is nothing to await.
    async def fail(_event: LineEvent) -> None:  # ruff: ignore[unused-async]
        """Raise *sentinel* when the returned coroutine is awaited."""
        raise sentinel

    return fail


def _failing_callback(callback_kind: str, sentinel: BaseException) -> LineHook:
    """Return the failing ``on_line`` hook for one case.

    The public ``LineHook`` alias is synchronous, while the hook the
    coordinator awaits is typed ``_LineHookFn``, whose outcome may itself be an
    awaitable. The async arm exercises that wider internal contract, which is
    the one this finding is about; the cast is the honest spelling of a runtime
    contract the public alias does not name.

    Returns
    -------
    LineHook
        A hook that raises *sentinel* when it is invoked or awaited.
    """
    if callback_kind == "sync":
        return _sync_callback(sentinel)
    return typ.cast("LineHook", _async_callback(sentinel))


async def _drive_failure(
    command: SafeCmd,
    output: RunOutputOptions,
    entry_point: str,
    sentinel: BaseException,
) -> None:
    """Exercise one entry point and assert the callback's error comes back."""

    async def invoke() -> None:
        """Drive the selected entry point to its callback failure."""
        if entry_point == "run":
            await command.run(output=output)
            return
        async for _event in command.lines(output=output):
            pass

    with pytest.raises(_CallbackError) as caught:
        await invoke()
    assert caught.value is sentinel, (
        f"{entry_point} must re-raise the callback's own exception, got "
        f"{caught.value!r}"
    )
    left = pending_tasks()
    # Names rather than the tasks themselves: a task has no ordering, so
    # ``sorted`` would raise precisely while reporting the leak.
    assert not left, (
        f"{entry_point} must leave no unfinished owned tasks behind, got "
        f"{sorted(task.get_name() for task in left)!r}"
    )


def _kill_leftovers(events: cabc.Sequence[ExecEvent]) -> None:
    """Force-kill any child this case left running, so a failure cannot leak it."""
    for pid in started_pids(events):
        if process_is_running(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


@pytest.mark.parametrize("entry_point", ["run", "lines"])
@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("callback_kind", ["sync", "async"])
def test_failing_output_callback_ends_the_run(
    entry_point: str,
    stream: str,
    callback_kind: str,
) -> None:
    """A callback failure ends the run without any configured timeout."""
    sentinel = _CallbackError(f"{entry_point} {stream} {callback_kind}")
    command = build_python_builder()("-c", _CHILD_SCRIPT.format(stream=stream))
    output = RunOutputOptions(on_line=_failing_callback(callback_kind, sentinel))
    events: list[ExecEvent] = []
    try:
        with sh.observe(events.append):
            try:
                asyncio.run(
                    asyncio.wait_for(
                        _drive_failure(command, output, entry_point, sentinel),
                        _WATCHDOG_S,
                    ),
                )
            except TimeoutError:
                pytest.fail(
                    f"{entry_point} did not end within {_WATCHDOG_S}s of a "
                    f"{callback_kind} {stream} callback failure, so it waited on "
                    "a child that could never exit"
                )
        pids = started_pids(events)
        assert len(pids) == 1, (
            f"{entry_point} must start exactly one child, got {pids!r}"
        )
        wait_for_process_death(
            pids[0],
            seconds=3.0,
            context=f"{entry_point} after a {callback_kind} {stream} failure",
        )
    finally:
        _kill_leftovers(events)
