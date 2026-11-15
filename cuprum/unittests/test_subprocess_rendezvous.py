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

import pytest

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


def test_successful_consumer_leaves_the_exit_wait_running() -> None:
    """A consumer that merely finishes does not end the run."""

    async def scenario() -> tuple[tuple[int, float], bool]:
        """Race a consumer that succeeds against the exit wait.

        Returns
        -------
        tuple[tuple[int, float], bool]
            The exit result, and whether the exit wait was cancelled.
        """
        probe = ExitProbe(exit_code=3, ticks=2)
        consumer = asyncio.create_task(asyncio.sleep(0, result=object()))
        result = await _await_exit_or_writer_failure(
            probe.wait(),
            None,
            consumers=(consumer,),
        )
        return result, probe.cancelled.is_set()

    result, cancelled = asyncio.run(scenario())
    assert result == (3, 1.5), f"the run must end on the exit, got {result!r}"
    assert not cancelled, "a consumer that succeeds must not cancel the exit wait"


def test_cancelled_consumer_leaves_the_exit_wait_running() -> None:
    """A cancelled consumer is a teardown, not a failure, and is not re-raised."""

    async def scenario() -> tuple[tuple[int, float], bool]:
        """Race a consumer that was cancelled before the race began.

        Returns
        -------
        tuple[tuple[int, float], bool]
            The exit result, and whether the exit wait was cancelled.
        """
        probe = ExitProbe(exit_code=4, ticks=1)
        consumer = asyncio.create_task(asyncio.sleep(0, result=object()))
        consumer.cancel()
        await retrieve(consumer)
        result = await _await_exit_or_writer_failure(
            probe.wait(),
            None,
            consumers=(consumer,),
        )
        return result, probe.cancelled.is_set()

    result, cancelled = asyncio.run(scenario())
    assert result == (4, 1.5), f"the run must end on the exit, got {result!r}"
    assert not cancelled, "a cancelled consumer must not cancel the exit wait"


def test_none_consumer_slots_are_skipped_and_the_other_is_still_raced() -> None:
    """A stream cuprum holds no pipe for is skipped, not waited on."""

    async def scenario() -> None:
        """Fail the stderr stream while the stdout slot holds no task."""
        probe = ExitProbe()
        sentinel = CallbackError("stderr callback failed")
        stderr_task = failed_task(sentinel)
        with pytest.raises(CallbackError) as caught:
            await _await_exit_or_writer_failure(
                probe.wait(),
                None,
                consumers=(None, stderr_task),
            )
        assert caught.value is sentinel, (
            f"the failed stream must still end the run, got {caught.value!r}"
        )
        assert probe.cancelled.is_set(), (
            "the failed stream must cancel the exit wait even when the other "
            "slot holds no task"
        )

    asyncio.run(scenario())


def test_consumer_failure_cancels_the_exit_and_raises_the_same_object() -> None:
    """A failed consumer ends the run at once, terminating a live child."""

    async def scenario() -> None:
        """Fail the only consumer against a child that never exits."""
        probe = ExitProbe()
        sentinel = CallbackError("callback failed")
        consumer = failed_task(sentinel)
        with pytest.raises(CallbackError) as caught:
            await _await_exit_or_writer_failure(
                probe.wait(),
                None,
                consumers=(consumer,),
            )
        assert caught.value is sentinel, (
            "the caller must receive the very object the consumer raised, got "
            f"{caught.value!r}"
        )
        assert probe.cancelled.is_set(), (
            "a consumer failure must cancel the exit wait, which is what "
            "terminates and reaps the child"
        )

    asyncio.run(scenario())


def test_stdin_failure_cancels_the_exit_wait_and_raises_the_same_object() -> None:
    """The default empty consumer sequence still races the stdin writer."""

    async def scenario() -> None:
        """Fail the stdin writer and leave the consumer sequence at its default."""
        probe = ExitProbe()
        sentinel = CallbackError("producer failed")
        stdin_task = failed_task(sentinel)
        with pytest.raises(CallbackError) as caught:
            await _await_exit_or_writer_failure(probe.wait(), stdin_task)
        assert caught.value is sentinel, (
            f"the caller must receive the writer's own error, got {caught.value!r}"
        )
        assert probe.cancelled.is_set(), "a writer failure must cancel the exit wait"

    asyncio.run(scenario())


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
        sentinel = CallbackError("callback failed")
        consumer = failed_task(sentinel)
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

    async def scenario() -> BaseException:
        """Fail the writer and a consumer in the same loop step.

        Returns
        -------
        BaseException
            Whichever failure the race chose to raise.
        """
        probe = ExitProbe()
        writer_sentinel = CallbackError("producer failed")
        consumer_sentinel = CallbackError("callback failed")
        stdin_task = failed_task(writer_sentinel)
        consumer = failed_task(consumer_sentinel)
        try:
            with pytest.raises(CallbackError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    stdin_task,
                    consumers=(consumer,),
                )
        finally:
            await retrieve(consumer)
        return caught.value

    raised = asyncio.run(scenario())
    assert str(raised) == "producer failed", (
        f"the stdin writer's failure must win, got {raised!r}"
    )


def test_stdout_failure_wins_over_a_stderr_failure() -> None:
    """The consumer pair's own order decides between two failed readers."""

    async def scenario() -> BaseException:
        """Fail both stream consumers in the same loop step.

        Returns
        -------
        BaseException
            Whichever failure the race chose to raise.
        """
        probe = ExitProbe()
        stdout_sentinel = CallbackError("stdout failed")
        stderr_sentinel = CallbackError("stderr failed")
        stdout_task = failed_task(stdout_sentinel)
        stderr_task = failed_task(stderr_sentinel)
        try:
            with pytest.raises(CallbackError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    None,
                    consumers=(stdout_task, stderr_task),
                )
        finally:
            await retrieve(stdout_task, stderr_task)
        return caught.value

    raised = asyncio.run(scenario())
    assert str(raised) == "stdout failed", (
        f"stdout's failure must be reported first, got {raised!r}"
    )


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


def test_consumer_failure_ignores_running_and_cancelled_tasks() -> None:
    """Only a settled, unabandoned failure counts as a consumer failure."""

    async def scenario() -> None:
        """Offer a running and a cancelled task to the failure query."""
        running = asyncio.create_task(asyncio.Event().wait())
        cancelled = asyncio.create_task(asyncio.sleep(0, result=object()))
        cancelled.cancel()
        await retrieve(cancelled)
        try:
            assert _consumer_failure((running, cancelled)) is None, (
                "a running or cancelled consumer must not read as a failure"
            )
        finally:
            running.cancel()
            await retrieve(running)

    asyncio.run(scenario())
