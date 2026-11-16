"""A failing output callback must end the run, not block on a full pipe.

A raising ``on_line`` callback ends the task draining the child's output. The
child keeps writing, nothing reads its pipe, and the exit the coordinator is
waiting for never arrives — so a run with no ``timeout=`` would wait forever.
Both entry points must instead notice the failed consumer, terminate and reap
the child, settle their siblings, and re-raise the callback's own exception.

Each case is bounded by a watchdog of its own rather than by a configured
execution timeout: the point is that no deadline is needed for the run to end.

Which failure the run reports is pinned here too. A producer that fails and
then parks in its own cleanup causes the callback failure that follows it, and
the caller's input contract must be answered with the source's error rather
than with the callback's.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import RunOutputOptions, StdinStream, sh
from cuprum.unittests._wait4_process_support import _abandon
from tests.helpers.catalogue import python_builder as build_python_builder
from tests.helpers.timeouts import (
    pending_tasks,
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


class _SourceError(RuntimeError):
    """The exception the stdin producer raises, and the run must report."""


class _ParkingSource:
    """A producer that fails once the callback has entered, then parks.

    The two events are what make the precedence testable without measuring
    child startup. Waiting for *entered* keeps the ordering explicit: the
    callback is already inside its call — and so already committed to failing
    once *parked* is set — before the producer raises. The park is the other
    half. A writer raises its failure and then finalizes this, the caller's
    code; while that is pending the writer's *task* still looks like a running
    task, which is precisely the window a reader of that task alone gets wrong.
    """

    def __init__(
        self,
        failure: BaseException,
        entered: asyncio.Event,
        parked: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        """Hold the failure to raise and the events it orders itself by."""
        self._failure = failure
        self._entered = entered
        self._parked = parked
        self._release = release

    def __aiter__(self) -> _ParkingSource:
        """Return this source, which is its own async iterator."""
        return self

    async def __anext__(self) -> str:
        """Fail on the first pull, once the callback is known to be running."""
        await self._entered.wait()
        raise self._failure

    async def aclose(self) -> None:
        """Report that cleanup has started, then stay parked until released."""
        self._parked.set()
        await self._release.wait()


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
    """Kill any child this case left running, so a failure cannot leak it.

    The probe is ``_abandon``'s rather than a liveness check on the pid: a
    reaped child's number can already belong to an unrelated process, and
    ``kill`` would then terminate a stranger. ``_abandon`` asks ``waitpid``
    what this process still owns and signals only a child that is live.
    """
    for pid in started_pids(events):
        _abandon(pid)


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


def test_source_failure_outranks_the_callback_failure_it_caused() -> None:
    """A producer's own failure is reported, not the callback's it caused.

    The callback is entered first, the producer fails once it is, and the
    callback then fails while the writer task is still parked in the producer's
    cleanup. Ordering it that way keeps child startup out of the requirement:
    the producer waits for the callback rather than for the child to have
    printed. The writer publishes its failure before it unwinds, so the run must
    report the source's ``StdinSourceError`` with the producer's exception
    chained — the failure the caller's input contract names — rather than the
    callback failure that followed it.
    """
    source_failure = _SourceError("producer failed")
    callback_failure = _CallbackError("callback failed")
    events: list[ExecEvent] = []

    async def drive() -> None:
        """Fail the producer, then the callback, and report the run's error."""
        callback_entered = asyncio.Event()
        parked = asyncio.Event()
        release = asyncio.Event()
        callback_raised = asyncio.Event()

        async def callback(_event: LineEvent) -> None:
            """Report entry, then fail once the producer's cleanup has parked."""
            callback_entered.set()
            await parked.wait()
            callback_raised.set()
            raise callback_failure

        command = build_python_builder()(
            "-c",
            "import time; print('line', flush=True); time.sleep(30)",
        )
        source = _ParkingSource(source_failure, callback_entered, parked, release)
        try:
            with pytest.raises(sh.StdinSourceError) as caught:
                await asyncio.wait_for(
                    command.run(
                        stdin=StdinStream(source),
                        # The public alias types the hook as synchronous; the
                        # coordinator awaits an awaitable outcome as well.
                        output=RunOutputOptions(
                            on_line=typ.cast("LineHook", callback),
                        ),
                    ),
                    _WATCHDOG_S,
                )
        finally:
            release.set()
            await asyncio.sleep(0)
        assert caught.value.__cause__ is source_failure, (
            "the run must report the producer's own failure, chained as the "
            f"cause, got {caught.value!r} caused by {caught.value.__cause__!r}"
        )
        assert callback_raised.is_set(), (
            "the callback must have failed, or this case does not test the "
            "precedence between the two failures"
        )
        assert not pending_tasks(), (
            "the run must leave no unfinished owned tasks behind, got "
            f"{sorted(task.get_name() for task in pending_tasks())!r}"
        )

    try:
        with sh.observe(events.append):
            asyncio.run(drive())
        pids = started_pids(events)
        assert len(pids) == 1, f"exactly one child must be started, got {pids!r}"
        wait_for_process_death(
            pids[0],
            seconds=3.0,
            context="after a source failure outranked the callback's",
        )
    finally:
        _kill_leftovers(events)
