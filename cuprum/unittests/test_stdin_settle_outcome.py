"""A settle that outlives its caller must not report against the loop.

Three sites await a stdin settle, or the writer itself, under a bound they may
not reach the end of: the streamed run's post-exit phase, the ``lines()``
post-exit drain, and the direct run's deadline-bounded writer await. Each lets
the caller's cancellation through immediately, leaving the awaited task running
after the caller has gone. When that task then fails — the writer failing as it
unwinds is the ordinary way it does — nothing is left to await it, so the
outcome has to be retrieved or the loop announces an unretrieved task
exception against a run that has already ended.

Retrieval alone is not enough, which is the part worth testing rather than
assuming. ``asyncio.shield`` detaches its own completion callback from a
still-pending inner task when the awaiting future is cancelled, and from Python
3.14 it re-registers ``asyncio.tasks._log_on_exception`` in its place. That
callback reports the inner failure through the loop however carefully the
outcome was retrieved, which is why these sites await through ``asyncio.wait``
instead. Only 3.14 reports: 3.12 and 3.13 stay silent under either spelling.

Every site is therefore checked for two things, because a quiet loop is
worthless if it was bought by swallowing the failure. A writer that fails after
the caller has left produces no report; a writer that fails before any
cancellation still reaches the caller as the very object it raised.

The reclaim inside ``_cancel_stdin_writer`` is covered here too. It is the
layer both streamed sites reach their writer through, and the outcome it
retrieves arrives after a bounded wait that has usually already expired — the
case a retrieval placed after that wait cannot see at all, because
``Task.exception()`` on a pending task raises ``InvalidStateError`` rather than
recording anything.
"""

from __future__ import annotations

import asyncio
import collections.abc as cabc
import gc
import time
import types
import typing as typ

import pytest

from cuprum._line_stream.drain import _await_writer_within_scope
from cuprum._subprocess_direct_run import _await_writer_within_deadline
from cuprum._subprocess_stdin_reclaim import (
    _STDIN_SETTLE_GRACE_S,
    _cancel_stdin_writer,
)
from cuprum._subprocess_stream_post_exit import _await_stdin_writer

if typ.TYPE_CHECKING:
    from cuprum._line_stream.line_queue import _LineStreamRun
    from cuprum._subprocess_spawn import _SubprocessExecution
    from cuprum._subprocess_wait_types import _RunTaskOwnership

# The direct run's configured deadline. Far longer than each case takes, so the
# scope is still open when the caller is cancelled and cannot be mistaken for
# the expiry the run would otherwise report.
_RUN_DEADLINE_S = 10.0

# An outer bound on the waits below, so a writer that never fails fails the
# case rather than hanging the suite. Far longer than anything being measured.
_LATE_FAILURE_TIMEOUT_S = 5.0


class _WriterError(RuntimeError):
    """Failure raised by the stand-in writers.

    Deliberately not a ``TimeoutError``. The direct run reads a ``TimeoutError``
    escaping its scope as the run's own expiry, and these cases are about
    whether a failure is retrieved rather than how a layer classifies it.
    """


def _reporting_loop() -> list[str]:
    """Install an exception handler that records what the loop is told.

    The handler is the witness. A leak is not something a coroutine can see —
    it is the loop reporting an outcome nobody retrieved — so the assertion is
    on what the loop was told rather than on any state the test can inspect.

    Returns
    -------
    list[str]
        The record the handler appends to, empty until something is reported.
    """
    reported: list[str] = []
    asyncio.get_running_loop().set_exception_handler(
        lambda _loop, context: reported.append(str(context.get("message")))
    )
    return reported


async def _fail_now(sentinel: BaseException) -> None:  # ruff: ignore[unused-async]
    """Raise ``sentinel`` as soon as the task is scheduled."""
    raise sentinel


class _LateWriter:
    """A writer that fails only when the test releases it.

    Releasing it after the caller has left is what places the failure outside
    the caller's reach. A sleep long enough to outlast the caller would be
    evidence about this machine's scheduler; an event the test sets itself is
    evidence that the failure genuinely landed afterwards.

    The test drops its own reference before the loop is given a chance to
    report: an unretrieved exception is announced when the task is collected,
    so nothing but the retrieval under test can keep this quiet.
    """

    def __init__(self) -> None:
        """Start with the writer parked and nothing released or finished."""
        self.released = asyncio.Event()
        self.finished = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """Schedule the writer and return its task."""
        self.task = asyncio.ensure_future(self._run())
        return self.task

    async def _run(self) -> None:
        """Wait to be released, then fail."""
        await self.released.wait()
        self.finished.set()
        msg = "the writer failed after its caller left"
        raise _WriterError(msg)

    def forget(self) -> None:
        """Drop the last reference the test holds to the task."""
        self.task = None


class _ReclaimedWriter:
    """A writer that absorbs its cancellation, then fails when released.

    The reclaim cancels this task and gives it a short budget to finish. Rather
    than unwinding, it parks — so the helper returns with the task still
    pending, which is precisely the state in which ``Task.exception()`` raises
    ``InvalidStateError`` and records nothing. The failure then arrives after
    the helper has returned, where only a callback attached to the task can
    retrieve it.
    """

    def __init__(self) -> None:
        """Start with the writer parked and nothing absorbed or released."""
        self.absorbed = asyncio.Event()
        self.released = asyncio.Event()
        self.finished = asyncio.Event()
        self.task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        """Schedule the writer and return its task."""
        self.task = asyncio.ensure_future(self._run())
        return self.task

    async def _run(self) -> None:
        """Absorb the cancellation, then fail once released."""
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.absorbed.set()
        await self.released.wait()
        self.finished.set()
        msg = "the writer failed as it unwound"
        raise _WriterError(msg)

    def forget(self) -> None:
        """Drop the last reference the test holds to the task."""
        self.task = None


def _ownership(stdin_task: asyncio.Task[None]) -> _RunTaskOwnership:
    """Return a stand-in for the run's task bookkeeping.

    The settle only reads the writer from it, so the stand-in carries exactly
    that and is typed as the bundle to satisfy the callee.

    Returns
    -------
    _RunTaskOwnership
        A ``types.SimpleNamespace`` carrying only the ``stdin_task`` field the
        settle reads.
    """
    return typ.cast("_RunTaskOwnership", types.SimpleNamespace(stdin_task=stdin_task))


def _run_record(stdin_task: asyncio.Task[None]) -> _LineStreamRun:
    """Return a stand-in for the line stream's run record.

    Returns
    -------
    _LineStreamRun
        A ``types.SimpleNamespace`` carrying the task bookkeeping the settle
        reads from the run record.
    """
    ownership = _ownership(stdin_task)
    return typ.cast("_LineStreamRun", types.SimpleNamespace(tasks=ownership))


def _execution() -> _SubprocessExecution:
    """Return a stand-in for the execution bundle the direct run reads.

    Only the deadline remainder is consulted, and it is measured from the
    bundle's own clock so the value handed to ``asyncio.timeout`` is a
    duration rather than a difference between two clocks.

    Returns
    -------
    _SubprocessExecution
        A stand-in carrying the configured timeout, the run's start reference,
        and the clock the remainder is measured with.
    """
    observation = types.SimpleNamespace(monotonic_clock=time.monotonic)
    fields = types.SimpleNamespace(
        timeout=_RUN_DEADLINE_S,
        started_at=time.monotonic(),
        observation=observation,
    )
    return typ.cast("_SubprocessExecution", fields)


async def _streamed_post_exit(stdin_task: asyncio.Task[None]) -> None:
    """Await the streamed run's post-exit settle."""
    await _await_stdin_writer(_ownership(stdin_task))


async def _line_stream_post_exit(stdin_task: asyncio.Task[None]) -> None:
    """Await the ``lines()`` post-exit settle."""
    await _await_writer_within_scope(_run_record(stdin_task))


async def _direct_run(stdin_task: asyncio.Task[None]) -> None:
    """Await the direct run's writer under its remaining deadline."""
    await _await_writer_within_deadline(_execution(), stdin_task)


type _Site = cabc.Callable[[asyncio.Task[None]], cabc.Awaitable[None]]

_SITES = (
    pytest.param(_streamed_post_exit, id="streamed-post-exit"),
    pytest.param(_line_stream_post_exit, id="lines-post-exit"),
    pytest.param(_direct_run, id="direct-run"),
)


async def _settle_until_cancelled(
    await_site: _Site,
    writer: asyncio.Task[None],
) -> None:
    """Run a site against ``writer`` and cancel it while the await is pending."""
    awaiting = asyncio.ensure_future(await_site(writer))
    # Let the site reach its wait before the cancellation lands; cancelled on
    # the first turn it would never have entered one.
    await asyncio.sleep(0)
    awaiting.cancel()
    try:
        await awaiting
    except asyncio.CancelledError:
        return
    msg = "the site returned instead of propagating the cancellation"
    raise AssertionError(msg)


async def _release_and_collect(writer: _LateWriter) -> None:
    """Let the writer fail, then give the loop every chance to report it."""
    writer.released.set()
    await asyncio.wait_for(writer.finished.wait(), timeout=_LATE_FAILURE_TIMEOUT_S)
    writer.forget()
    # The report this module is about comes from collecting a task that still
    # carries an exception, so the reference has to go before it can appear.
    for _ in range(4):
        gc.collect()
        await asyncio.sleep(0.02)


@pytest.mark.parametrize("await_site", _SITES)
def test_a_writer_failing_after_its_caller_leaves_is_not_reported(
    await_site: _Site,
) -> None:
    """A writer that fails once its caller has gone is retrieved, not reported.

    The caller is cancelled while the await is still pending, which is what
    every deadline and every teardown cancellation does. The writer is left
    running by design — that is the latency property the shield exists for —
    so the failure it raises afterwards has no caller to reach, and retrieving
    it is the only way the loop is not told about a writer cuprum is finished
    with.

    The two settle sites are cancelled inside their grace window, so the settle
    task itself then fails; the direct run awaits the writer directly and the
    writer fails. Either way the failing task is one no frame holds by the time
    it ends.
    """
    writer = _LateWriter()

    async def scenario() -> tuple[list[str], bool]:
        """Cancel the site's caller, then let the writer fail."""
        reported = _reporting_loop()
        task = writer.start()
        await asyncio.sleep(0)
        await _settle_until_cancelled(await_site, task)
        await _release_and_collect(writer)
        return reported, writer.finished.is_set()

    reported, did_fail = asyncio.run(scenario())
    assert did_fail, (
        "the writer never failed, so the cancellation reached it as a reclaim "
        "instead and this case exercised nothing"
    )
    assert not reported, (
        "the loop reported an outcome nothing retrieved, which is the leak "
        f"this module exists to prevent: {reported}"
    )


@pytest.mark.parametrize("await_site", _SITES)
def test_a_writer_failing_before_the_cancellation_reaches_the_caller(
    await_site: _Site,
) -> None:
    """Retrieval must not turn a real writer failure into silence.

    The failure lands before any cancellation, so the caller is still there to
    receive it. This is the control for the case above: without it, a site
    that swallowed everything would satisfy it too.
    """
    sentinel = _WriterError("the writer failed while its caller was waiting")

    async def scenario() -> None:
        """Await a writer that fails before anything cancels the caller."""
        writer = asyncio.ensure_future(_fail_now(sentinel))
        await asyncio.sleep(0)
        await await_site(writer)

    with pytest.raises(_WriterError) as caught:
        asyncio.run(scenario())
    assert caught.value is sentinel, (
        "the caller must receive the very object the writer raised, got "
        f"{caught.value!r}"
    )


def test_a_reclaimed_writer_that_fails_after_its_budget_is_not_reported() -> None:
    """A writer reclaimed past its budget is retrieved when it later fails.

    This is the layer both streamed sites reach their writer through, and the
    case a retrieval placed *after* the bounded wait cannot cover: when the
    budget expires the writer is still pending, where ``Task.exception()``
    raises ``InvalidStateError`` and marks nothing. A writer that then fails as
    it unwinds is reported as never retrieved. The release happens strictly
    after the helper has returned, so the ordering is the test's rather than
    the scheduler's.
    """
    writer = _ReclaimedWriter()

    async def scenario() -> tuple[list[str], bool]:
        """Reclaim a writer that parks instead of unwinding, then fail it."""
        reported = _reporting_loop()
        task = writer.start()
        await asyncio.sleep(0)
        await _cancel_stdin_writer(task, budget=_STDIN_SETTLE_GRACE_S / 8)
        absorbed = writer.absorbed.is_set()
        writer.released.set()
        await asyncio.wait_for(writer.finished.wait(), timeout=_LATE_FAILURE_TIMEOUT_S)
        writer.forget()
        for _ in range(4):
            gc.collect()
            await asyncio.sleep(0.02)
        return reported, absorbed

    reported, absorbed = asyncio.run(scenario())
    assert absorbed, (
        "the writer never absorbed its cancellation, so the helper did not "
        "reclaim it and this case exercises the wrong path"
    )
    assert not reported, (
        "the loop reported a reclaimed writer's failure, which is what a "
        f"retrieval after the bounded wait cannot prevent: {reported}"
    )
