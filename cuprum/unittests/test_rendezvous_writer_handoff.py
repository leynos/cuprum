"""A writer's failure must be visible before its own cleanup finishes.

The exit race has two windows, and asking the writer's task answers neither.
A writer publishes its failure before it unwinds, so a run that ends while the
writer is still finalizing the caller's producer must still report the
writer's error rather than the consumer failure it caused. And a writer whose
failure surfaces only while the child is being torn down is found by the race's
second read of the precedence. These tests pin both, without starting a child.
"""

from __future__ import annotations

import asyncio

import pytest

from cuprum._subprocess_rendezvous import (
    _await_exit_or_writer_failure,
    _first_early_failure,
    _is_early_failure,
    _StdinWriterLog,
)
from cuprum.unittests._rendezvous_support import (
    CallbackError,
    ExitProbe,
    failed_task,
    retrieve,
)


def test_published_failure_wins_while_the_writer_task_still_runs() -> None:
    """A writer that has raised but not settled is still the run's outcome."""

    async def scenario() -> None:
        """Park the writer's task and fail a consumer beside it."""
        probe = ExitProbe()
        writer_failure = CallbackError("producer failed")
        consumer_failure = CallbackError("callback failed")
        log = _StdinWriterLog()
        log.publish(writer_failure)
        writer = asyncio.create_task(asyncio.Event().wait())
        consumer = failed_task(consumer_failure)
        try:
            with pytest.raises(CallbackError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    writer,
                    consumers=(consumer,),
                    stdin_log=log,
                )
            still_unwinding = not writer.done()
            # What the race would have read without the publication: the task
            # has not failed yet, so the consumer's error is all it can see.
            without_log = _first_early_failure(writer, (consumer,))
        finally:
            writer.cancel()
            await retrieve(writer, consumer)
        assert caught.value is writer_failure, (
            f"the published failure must be raised, got {caught.value!r}"
        )
        assert still_unwinding, (
            "the writer must still be unwinding when the race reads it, or "
            "this case does not test the window it claims to"
        )
        assert without_log is consumer_failure, (
            "the writer's task must still read as running, or the publication "
            "is not what selected the failure"
        )
        assert probe.cancelled.is_set(), (
            "the writer's failure must cancel the exit wait, which is what "
            "terminates and reaps the child"
        )
        assert _is_early_failure(
            writer_failure,
            writer,
            (consumer,),
            stdin_log=log,
        ), "the predicate must recognize a failure that is only published"

    asyncio.run(scenario())


def test_writer_failure_settled_by_the_teardown_wins_over_the_consumer() -> None:
    """A writer released by the exit wait's cancellation outranks the consumer.

    The first read cannot see this writer: it fails only once the teardown
    starts. Removing the second read makes the race report the consumer's
    error, which is what this case exists to catch.
    """

    async def scenario() -> None:
        """Fail the writer only once the exit wait has been cancelled."""
        settled = asyncio.Event()
        probe = ExitProbe(cancel_waits=settled)
        writer_failure = CallbackError("producer failed")
        consumer_failure = CallbackError("callback failed")

        async def writer_body() -> object:
            """Fail once the child is being torn down, as a release would."""
            await probe.cancelled.wait()
            settled.set()
            raise writer_failure

        writer = asyncio.create_task(writer_body())
        consumer = failed_task(consumer_failure)
        try:
            with pytest.raises(CallbackError) as caught:
                await _await_exit_or_writer_failure(
                    probe.wait(),
                    writer,
                    consumers=(consumer,),
                )
        finally:
            await retrieve(writer, consumer)
        assert caught.value is writer_failure, (
            f"the writer's failure must win after the teardown, got {caught.value!r}"
        )
        assert probe.cancelled.is_set(), (
            "the consumer's failure must cancel the exit wait, which is what "
            "releases the writer"
        )

    asyncio.run(scenario())
