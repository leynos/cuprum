"""The exit rendezvous races the child against its stdin and stream tasks.

A child that keeps producing output after a stream task has failed cannot exit:
nothing drains its pipe. These tests pin the race that ends such a run, and the
precedence it applies when more than one monitored task has failed, without
starting a child at all — the end-to-end lifecycle lives in
``test_consumer_failure_fail_fast.py`` and the entry points' own timeout
attribution in ``test_line_stream_reader_failure.py``.
"""

from __future__ import annotations

import asyncio
import enum
import typing as typ

import pytest

from cuprum._stream_consumer_errors import ConsumerCancelledError
from cuprum._stream_drain_finish import _CapturedDrain
from cuprum._subprocess_rendezvous import (
    _await_exit_or_writer_failure,
    _consumer_failure,
    _is_early_failure,
)
from cuprum.unittests._rendezvous_support import (
    CallbackError,
    ExitProbe,
    failed_task,
    retrieve,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def _task_for(sentinel: BaseException | None) -> asyncio.Task[object] | None:
    """Build an already-failed task for *sentinel*, or no task for ``None``."""
    return None if sentinel is None else failed_task(sentinel)


async def _race_failing_tasks(
    stdin_message: str | None,
    consumer_messages: cabc.Sequence[str | None],
) -> tuple[BaseException, ExitProbe, tuple[BaseException | None, ...]]:
    """Race failing tasks into the rendezvous and report what it selected.

    The first message names the stdin writer's failure and the rest name the
    consumers' in stdout-then-stderr order; ``None`` marks a stream cuprum
    holds no pipe for, whose slot therefore holds no task. Returning the
    sentinels in that same order lets a case assert identity rather than
    compare messages. Every task created here is retrieved before returning, so
    a failing assertion in the caller cannot leave one pending.

    Returns
    -------
    tuple[BaseException, ExitProbe, tuple[BaseException | None, ...]]
        The failure the race raised, the probe it raced against, and the
        sentinels in writer-then-consumers order.
    """
    sentinels = tuple(
        None if message is None else CallbackError(message)
        for message in (stdin_message, *consumer_messages)
    )
    stdin_task = _task_for(sentinels[0])
    consumers = tuple(_task_for(sentinel) for sentinel in sentinels[1:])
    probe = ExitProbe()
    try:
        with pytest.raises(CallbackError) as caught:
            await _await_exit_or_writer_failure(
                probe.wait(),
                stdin_task,
                consumers=consumers,
            )
    finally:
        tasks = (stdin_task, *consumers)
        await retrieve(*[task for task in tasks if task is not None])
    return caught.value, probe, sentinels


def test_exit_with_nothing_to_race_is_awaited_directly() -> None:
    """No writer and no consumer means the exit wait is awaited as it is."""

    async def scenario() -> tuple[int, float]:
        """Race an exit wait that has nothing to monitor.

        Returns
        -------
        tuple[int, float]
            The exit code and timestamp the probe reports.
        """
        probe = ExitProbe(exit_code=7)
        probe.release.set()
        return await _await_exit_or_writer_failure(
            probe.wait(),
            None,
            consumers=(None, None),
        )

    assert asyncio.run(scenario()) == (7, 1.5), (
        "an unobserved run must return the exit wait's own result"
    )


class _ConsumerOutcome(enum.Enum):
    """How one settled consumer ended, for the classification table."""

    SUCCEEDS = "succeeds"
    CANCELLED = "cancelled"
    SHORT_STOPPED = "short_stopped"


async def _settled_consumer(outcome: _ConsumerOutcome) -> asyncio.Task[object]:
    """Build a consumer that is already settled in *outcome*'s way.

    Returns
    -------
    asyncio.Task[object]
        The settled consumer task.
    """
    if outcome is _ConsumerOutcome.SHORT_STOPPED:
        return asyncio.create_task(
            asyncio.sleep(
                0,
                result=_CapturedDrain(payload="partial", reached_eof=False),
            ),
        )
    consumer = asyncio.create_task(asyncio.sleep(0, result=object()))
    if outcome is _ConsumerOutcome.CANCELLED:
        consumer.cancel()
        await retrieve(consumer)
    return consumer


def _slot_consumers(
    consumer: asyncio.Task[object],
    slot: int,
) -> tuple[asyncio.Task[object] | None, ...]:
    """Place *consumer* in one stream's slot, leaving the other absent."""
    return tuple(consumer if index == slot else None for index in range(2))


async def _race_settled_consumer(
    outcome: _ConsumerOutcome,
    slot: int,
) -> tuple[tuple[int, float] | None, ConsumerCancelledError | None, bool]:
    """Race one settled consumer against an exit wait.

    Returns
    -------
    tuple[tuple[int, float] | None, ConsumerCancelledError | None, bool]
        The exit result for a quiet consumer, the run's error for a cancelled
        or short-stopped one, and whether the exit wait was cancelled.
    """
    # A quiet consumer must be outlasted by the exit; a failing one needs an
    # exit that stays pending, or the settled exit would win outright.
    probe = (
        ExitProbe(exit_code=4, ticks=2)
        if outcome is _ConsumerOutcome.SUCCEEDS
        else ExitProbe()
    )
    consumer = await _settled_consumer(outcome)
    result: tuple[int, float] | None = None
    failure: ConsumerCancelledError | None = None
    try:
        if outcome is _ConsumerOutcome.SUCCEEDS:
            result = await _await_exit_or_writer_failure(
                probe.wait(),
                None,
                consumers=_slot_consumers(consumer, slot),
            )
        else:
            with pytest.raises(ConsumerCancelledError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    None,
                    consumers=_slot_consumers(consumer, slot),
                )
            failure = caught.value
    finally:
        await retrieve(consumer)
    return result, failure, probe.cancelled.is_set()


@pytest.mark.parametrize(
    ("outcome", "slot", "stream"),
    [
        (_ConsumerOutcome.SUCCEEDS, 0, "stdout"),
        (_ConsumerOutcome.CANCELLED, 0, "stdout"),
        (_ConsumerOutcome.CANCELLED, 1, "stderr"),
        (_ConsumerOutcome.SHORT_STOPPED, 0, "stdout"),
        (_ConsumerOutcome.SHORT_STOPPED, 1, "stderr"),
    ],
)
def test_a_settled_consumer_outcome_is_classified(
    outcome: _ConsumerOutcome,
    slot: int,
    stream: str,
) -> None:
    """A quiet consumer leaves the exit alone; a cancelled one ends the run."""
    result, failure, cancelled = asyncio.run(_race_settled_consumer(outcome, slot))
    if outcome is _ConsumerOutcome.SUCCEEDS:
        assert result == (4, 1.5), f"the run must end on the exit, got {result!r}"
        assert failure is None, (
            f"a quiet consumer must not fail the run, got {failure!r}"
        )
        assert not cancelled, "a consumer that succeeded must not cancel the exit wait"
        return
    assert failure is not None, "a cancelled consumer must end the run with an error"
    assert failure.stream == stream, (
        f"the {stream} consumer's outcome must name its stream, got {failure.stream!r}"
    )
    assert cancelled, (
        "a cancelled consumer must cancel the exit wait, which is what "
        "terminates and reaps the child"
    )
    if outcome is _ConsumerOutcome.CANCELLED:
        assert isinstance(failure.__cause__, asyncio.CancelledError), (
            "the consumer's own cancellation must be chained as the cause, got "
            f"{failure.__cause__!r}"
        )
    else:
        assert failure.__cause__ is None, (
            "a drain that returned has no cancellation to chain, got "
            f"{failure.__cause__!r}"
        )


def test_a_cancelled_writer_stays_quiet() -> None:
    """A cancelled stdin writer is teardown, not a failure."""

    async def scenario() -> tuple[tuple[int, float], bool]:
        """Race a cancelled writer and a successful consumer against the exit.

        Returns
        -------
        tuple[tuple[int, float], bool]
            The exit result, and whether the exit wait was cancelled.
        """
        probe = ExitProbe(exit_code=4, ticks=2)
        writer = asyncio.create_task(asyncio.Event().wait())
        writer.cancel()
        await retrieve(writer)
        consumer = asyncio.create_task(asyncio.sleep(0, result=object()))
        result = await _await_exit_or_writer_failure(
            probe.wait(),
            writer,
            consumers=(consumer, None),
        )
        return result, probe.cancelled.is_set()

    result, cancelled = asyncio.run(scenario())
    assert result == (4, 1.5), f"the run must end on the exit, got {result!r}"
    assert not cancelled, "a cancelled writer must not cancel the exit wait"


def test_a_cancelled_consumer_outranks_a_cancelled_writer() -> None:
    """The writer's quiet cancellation cannot hide the consumer's failure."""

    async def scenario() -> ConsumerCancelledError:
        """Cancel both the writer and one consumer before the race.

        Returns
        -------
        ConsumerCancelledError
            The failure the run reports.
        """
        probe = ExitProbe()
        writer = asyncio.create_task(asyncio.Event().wait())
        consumer = asyncio.create_task(asyncio.Event().wait())
        writer.cancel()
        consumer.cancel()
        await retrieve(writer, consumer)
        with pytest.raises(ConsumerCancelledError) as caught:
            await _await_exit_or_writer_failure(
                probe.wait(),
                writer,
                consumers=(consumer, None),
            )
        return caught.value

    failure = asyncio.run(scenario())
    assert failure.stream == "stdout", (
        f"the consumer's stream must name the failure, got {failure.stream!r}"
    )


def test_none_consumer_slots_are_skipped_and_the_other_is_still_raced() -> None:
    """A stream cuprum holds no pipe for is skipped, not waited on."""
    raised, probe, (_, _, stderr) = asyncio.run(
        _race_failing_tasks(None, (None, "stderr callback failed")),
    )
    assert raised is stderr, f"the failed stream must still end the run, got {raised!r}"
    assert probe.cancelled.is_set(), (
        "the failed stream must cancel the exit wait even when the other slot "
        "holds no task"
    )


def test_consumer_failure_cancels_the_exit_and_raises_the_same_object() -> None:
    """A failed consumer ends the run at once, terminating a live child."""
    raised, probe, (_, consumer) = asyncio.run(
        _race_failing_tasks(None, ("callback failed",)),
    )
    assert raised is consumer, (
        f"the caller must receive the very object the consumer raised, got {raised!r}"
    )
    assert probe.cancelled.is_set(), (
        "a consumer failure must cancel the exit wait, which is what terminates "
        "and reaps the child"
    )


def test_stdin_failure_cancels_the_exit_wait_and_raises_the_same_object() -> None:
    """The default empty consumer sequence still races the stdin writer."""
    raised, probe, (writer,) = asyncio.run(
        _race_failing_tasks("producer failed", ()),
    )
    assert raised is writer, (
        f"the caller must receive the writer's own error, got {raised!r}"
    )
    assert probe.cancelled.is_set(), "a writer failure must cancel the exit wait"


def test_settled_exit_wins_over_a_consumer_failing_in_the_same_step() -> None:
    """An exit that settles in the same step is the run's outcome."""

    async def scenario() -> tuple[tuple[int, float], BaseException | None]:
        """Race an exit that settles at once against a failing consumer.

        Returns
        -------
        tuple[tuple[int, float], BaseException | None]
            The race's result, and the failure the consumer still carries.
        """
        probe = ExitProbe(exit_code=9, ticks=0)
        consumer = failed_task(CallbackError("callback failed"))
        try:
            result = await _await_exit_or_writer_failure(
                probe.wait(),
                None,
                consumers=(consumer,),
            )
        finally:
            await retrieve(consumer)
        return result, consumer.exception()

    result, failure = asyncio.run(scenario())
    assert result == (9, 1.5), f"the settled exit must win, got {result!r}"
    assert failure is not None, (
        "the consumer must still have failed, or this case does not test the "
        "precedence it claims to"
    )


def test_stdin_failure_wins_over_a_consumer_failure() -> None:
    """The writer's failure is preferred when both fail in the same step."""
    raised, _, (writer, _consumer) = asyncio.run(
        _race_failing_tasks("producer failed", ("callback failed",)),
    )
    assert raised is writer, f"the stdin writer's failure must win, got {raised!r}"


def test_stdout_failure_wins_over_a_stderr_failure() -> None:
    """The consumer pair's own order decides between two failed readers."""
    raised, _, (_, stdout, _stderr) = asyncio.run(
        _race_failing_tasks(None, ("stdout failed", "stderr failed")),
    )
    assert raised is stdout, f"stdout's failure must be reported first, got {raised!r}"


def test_teardown_induced_reader_failure_does_not_replace_the_selected_one() -> None:
    """A reader that fails only during termination is not the run's outcome.

    Termination makes the child's pipes reach EOF, so a reader still draining
    can fail on what that leaves behind — a strict decoder flushing a sequence
    the termination truncated is the shape this models. stdout outranks stderr,
    so reading the consumers again after the teardown would report that
    artefact in place of the callback failure on stderr that ended the run.
    """
    callback_sentinel = CallbackError("stderr callback failed")

    async def scenario() -> tuple[BaseException, BaseException | None]:
        """Fail stderr, then fail stdout with the teardown's own artefact.

        Returns
        -------
        tuple[BaseException, BaseException | None]
            Whichever failure the race raised, and the failure stdout actually
            ended on.
        """
        teardown_error = CallbackError("truncated sequence")
        stdout_failed = asyncio.Event()
        probe = ExitProbe(cancel_waits=stdout_failed)

        async def fail_during_teardown() -> object:
            """Fail as a draining reader would, once the child is torn down."""
            await probe.cancelled.wait()
            stdout_failed.set()
            raise teardown_error

        stdout_task = asyncio.create_task(fail_during_teardown())
        stderr_task = failed_task(callback_sentinel)
        try:
            with pytest.raises(CallbackError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    None,
                    consumers=(stdout_task, stderr_task),
                )
        finally:
            await retrieve(stdout_task, stderr_task)
        return caught.value, stdout_task.exception()

    raised, teardown_failure = asyncio.run(scenario())
    assert teardown_failure is not None, (
        "stdout must have failed during the teardown, or this case does not "
        "test what the second read would have replaced the callback error with"
    )
    assert raised is callback_sentinel, (
        "the callback failure selected before the termination must survive it, "
        f"got {raised!r}"
    )


def test_caller_cancellation_cancels_and_settles_the_exit_wait() -> None:
    """A cancellation reaching the race is forwarded to the exit wait."""

    async def scenario() -> None:
        """Cancel the racing task while the exit wait is still pending."""
        probe = ExitProbe()
        consumer = asyncio.create_task(asyncio.Event().wait())
        race = asyncio.create_task(
            _await_exit_or_writer_failure(
                probe.wait(),
                None,
                consumers=(consumer,),
            ),
        )
        try:
            await probe.started.wait()
            race.cancel()
            with pytest.raises(asyncio.CancelledError):
                await race
            assert probe.cancelled.is_set(), (
                "a caller's cancellation must still terminate the child"
            )
        finally:
            consumer.cancel()
            await retrieve(race, consumer)

    asyncio.run(scenario())


def test_provenance_predicate_matches_only_monitored_failures() -> None:
    """Identity, not type or message, decides whether a failure is monitored."""

    async def scenario() -> None:
        """Ask the predicate about a monitored failure and its lookalike."""
        sentinel = CallbackError("callback failed")
        stdin_task = failed_task(sentinel)
        consumer = failed_task(sentinel)
        lookalike = CallbackError("callback failed")
        await retrieve(stdin_task, consumer)
        assert _is_early_failure(sentinel, stdin_task, (consumer,)), (
            "a monitored task's own failure must be recognized"
        )
        assert not _is_early_failure(lookalike, stdin_task, (consumer,)), (
            "an equal-looking exception from elsewhere is not a monitored failure"
        )
        assert not _is_early_failure(sentinel, None, (None, None)), (
            "a run with no monitored tasks can have no monitored failure"
        )

    asyncio.run(scenario())


def test_consumer_failure_ignores_running_and_reports_cancelled_tasks() -> None:
    """A running consumer is quiet; a cancelled one is a labelled failure."""

    async def scenario() -> None:
        """Offer running and cancelled consumers to the failure query."""
        running = asyncio.create_task(asyncio.Event().wait())
        cancelled = asyncio.create_task(asyncio.sleep(0, result=object()))
        cancelled.cancel()
        await retrieve(cancelled)
        try:
            assert _consumer_failure((running, None)) is None, (
                "a running consumer must not read as a failure"
            )
            failure = _consumer_failure((None, cancelled))
            assert isinstance(failure, ConsumerCancelledError), (
                "a cancelled consumer must read as a stream-labelled failure, "
                f"got {failure!r}"
            )
            assert failure.stream == "stderr", (
                f"the failure must name stderr, got {failure.stream!r}"
            )
            assert isinstance(failure.__cause__, asyncio.CancelledError), (
                "the cancelled task's own cancellation must be chained, got "
                f"{failure.__cause__!r}"
            )
        finally:
            running.cancel()
            await retrieve(running)

    asyncio.run(scenario())
