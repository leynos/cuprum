"""A stop and a settle are weighed together, and the caller is owed one error.

``cuprum/_process_handle/settle.py`` runs the stop and the settle as two
shielded steps and then decides which failure, if any, the caller sees. The
rule is stated once in its module docstring — a cancellation outranks any
failure, and a stop's failure outranks a settle's — and until now no test
pinned it. That matters more than usual here because the two callers differ
only in *whether* an ordinary failure may be deferred, so a review that
checked one caller proves nothing about the other.

These tests drive the seam directly rather than through a run. The module
docstring says the functions take the settle as a factory rather than a handle
so that exactly this is possible, and every case here is coordinated by an
exception object rather than a guessed interval.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum._process_handle.settle import _settle_after_stop, _settle_teardown
from cuprum._process_lifecycle import _NO_STOP
from cuprum.sh import CommandResult, Program

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._process_lifecycle import _TerminationOutcome

#: A result the settle is free to return, since nothing here reads its fields.
_ANY_RESULT = CommandResult(Program("true"), (), 0, 1, None, None)

_STOP_RECORD: tuple[None, _TerminationOutcome] = (None, _NO_STOP)
"""A stop record whose measurement is ``None`` — the unobserved case."""


async def _succeeding_stop() -> tuple[None, _TerminationOutcome]:
    """Return the unobserved stop record, suspending once as a real stop does."""
    await asyncio.sleep(0)
    return _STOP_RECORD


async def _failing_stop(error: BaseException) -> tuple[None, _TerminationOutcome]:
    """Raise *error* from a stop, suspending once like a real stop."""
    await asyncio.sleep(0)
    raise error


def _settle() -> cabc.Callable[[], cabc.Awaitable[CommandResult]]:
    """Return a settle factory that succeeds with the shared result."""

    async def settle() -> CommandResult:
        """Suspend once, then return the shared result."""
        await asyncio.sleep(0)
        return _ANY_RESULT

    return settle


def _failing_settle(
    error: BaseException,
) -> cabc.Callable[[], cabc.Awaitable[CommandResult]]:
    """Return a settle factory that raises *error*."""

    async def settle() -> CommandResult:
        """Suspend once, then raise the supplied error."""
        await asyncio.sleep(0)
        raise error

    return settle


class TestCancellationOutranksFailure:
    """A cancellation is a request to stop, not a failure to report."""

    def test_a_cancelled_stop_outranks_a_successful_settle(self) -> None:
        """The stop's cancellation reaches the caller even though settling worked."""

        async def run_case() -> None:
            """Cancel the stop and check it is what propagates."""
            cancellation = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await _settle_after_stop(_failing_stop(cancellation), _settle())

        asyncio.run(run_case())

    def test_a_cancelled_settle_outranks_a_successful_stop(self) -> None:
        """The settle's cancellation is owed even when the stop delivered."""

        async def run_case() -> None:
            """Cancel the settle and check it is what propagates."""
            cancellation = asyncio.CancelledError()
            with pytest.raises(asyncio.CancelledError):
                await _settle_after_stop(
                    _succeeding_stop(), _failing_settle(cancellation)
                )

        asyncio.run(run_case())

    def test_a_cancellation_outranks_an_ordinary_failure(self) -> None:
        """A cancelled stop is reported, not the settle error it arrived beside.

        This is the ordering the module docstring calls out: the ordinary
        failure is the one fact a caller may legitimately be told to ignore, so
        letting it displace the cancellation would lose the stop request.
        """

        async def run_case() -> None:
            """Cancel the stop beside a settle error and check the cancel wins."""
            cancellation = asyncio.CancelledError()
            message = "settle failed"
            with pytest.raises(asyncio.CancelledError):
                await _settle_after_stop(
                    _failing_stop(cancellation), _failing_settle(RuntimeError(message))
                )

        asyncio.run(run_case())


class TestStopFailureOutranksSettleFailure:
    """Two ordinary failures are not equally owed."""

    def test_the_stops_failure_is_the_one_raised(self) -> None:
        """The caller asked for the stop, so its failure is the one reported."""

        async def run_case() -> None:
            """Fail both steps and check the stop's error is raised."""
            stop_message = "stop failed"
            settle_message = "settle failed"
            with pytest.raises(RuntimeError, match=stop_message):
                await _settle_after_stop(
                    _failing_stop(RuntimeError(stop_message)),
                    _failing_settle(RuntimeError(settle_message)),
                )

        asyncio.run(run_case())

    def test_a_stop_that_delivered_still_reports_its_measurement(self) -> None:
        """A stop that returned does not mask the settle failure that follows."""

        async def run_case() -> None:
            """Return from the stop, fail the settle, check the settle error."""
            message = "settle failed"
            with pytest.raises(RuntimeError, match=message):
                await _settle_after_stop(
                    _succeeding_stop(), _failing_settle(RuntimeError(message))
                )

        asyncio.run(run_case())


class TestTeardownDefersToTheBlocksError:
    """A teardown failure must not replace the error that caused the exit."""

    def test_an_ordinary_failure_is_suppressed_when_the_block_already_failed(
        self,
    ) -> None:
        """With a pending exception the teardown stays silent, as documented."""

        async def run_case() -> None:
            """Fail the teardown with a block error pending and check silence."""
            stop_message = "teardown stop failed"
            block_message = "block failed"
            # Must not raise: the block's own error is what the caller sees,
            # and the teardown has nothing to add to it.
            await _settle_teardown(
                _failing_stop(RuntimeError(stop_message)),
                _settle(),
                RuntimeError(block_message),
            )

        asyncio.run(run_case())

    def test_an_ordinary_failure_reaches_the_caller_when_the_block_succeeded(
        self,
    ) -> None:
        """With no pending exception the teardown failure is the only one left."""

        async def run_case() -> None:
            """Fail the teardown with no block error and check it propagates."""
            message = "teardown stop failed"
            with pytest.raises(RuntimeError, match=message):
                await _settle_teardown(
                    _failing_stop(RuntimeError(message)), _settle(), None
                )

        asyncio.run(run_case())

    def test_a_cancellation_is_never_suppressed(self) -> None:
        """A pending block error does not let a cancellation be swallowed.

        ``_shielded_cleanup`` finishes the teardown and re-raises the
        cancellation it absorbed, so suppressing it because the block already
        failed would leave the task marked cancelled with no matching
        ``CancelledError`` — the state an enclosing timeout or task group then
        waits on forever.
        """

        async def run_case() -> None:
            """Cancel the teardown with a block error pending; check it propagates."""
            cancellation = asyncio.CancelledError()
            block_message = "block failed"
            with pytest.raises(asyncio.CancelledError):
                await _settle_teardown(
                    _failing_stop(cancellation),
                    _settle(),
                    RuntimeError(block_message),
                )

        asyncio.run(run_case())
