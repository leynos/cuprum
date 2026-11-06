"""A failed output read during startup abandonment must not be lost.

The reclaim's discard step reads both output streams to end-of-file under one
grace. A read can fail in its own right — a reset pipe is the ordinary case —
and the module's contract is that such a failure reaches an operator through
the same ``output_discard`` report the reclaim uses for a drain that timed out.
``asyncio.gather(..., return_exceptions=True)`` deliberately keeps one stream's
failure from cancelling the other, so the failure has to be collected and
re-raised rather than left in the results: dropped, it would leave only the
transport close behind it and no record of why that close was needed.

Read stubs are used rather than a spawned child, because the question here is
what the discard does with a failure, not how a pipe comes to fail.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ
from unittest import mock

import pytest

from cuprum import _subprocess_startup_abandon as reclaim
from cuprum._subprocess_startup_abandon import _discard_outputs

if typ.TYPE_CHECKING:
    from cuprum._subprocess_execution import _SubprocessExecution


class _ReadFailureError(OSError):
    """A read that failed, as a reset pipe's would."""


class _RaisingReader:
    """A stream whose read fails outright, after recording that it was entered."""

    def __init__(self, error: BaseException, entered: list[str], name: str) -> None:
        """Bind the failure and the entry log this reader contributes to."""
        self._error = error
        self._entered = entered
        self._name = name

    async def read(self, _size: int) -> bytes:
        """Record the entry, then fail as the real pipe would."""
        self._entered.append(self._name)
        raise self._error


def test_a_failed_read_is_raised_after_the_other_stream_settles() -> None:
    """The discard must surface a read failure, not return as if it drained.

    Both readers are entered inside the one grace, so raising the first
    failure only after the gather proves the other stream's read was still
    settled rather than cancelled by its neighbour's failure. Stream order
    decides which of two failures the caller sees, so the report names a
    stable step rather than whichever read lost the race.
    """

    async def run_case() -> None:
        """Discard two streams whose reads both fail."""
        entered: list[str] = []
        stdout_error = _ReadFailureError("stdout was reset")
        stderr_error = _ReadFailureError("stderr was reset")
        pipes = types.SimpleNamespace(
            stdout=_RaisingReader(stdout_error, entered, "stdout"),
            stderr=_RaisingReader(stderr_error, entered, "stderr"),
        )

        with pytest.raises(_ReadFailureError) as raised:
            await _discard_outputs(typ.cast("asyncio.subprocess.Process", pipes))

        assert raised.value is stdout_error, (
            "the first ordinary failure in stream order must be the one raised"
        )
        assert entered == ["stdout", "stderr"], (
            "a failure on one stream must not cancel the other stream's read"
        )

    asyncio.run(run_case())


def test_a_failed_read_is_reported_and_the_transports_still_close() -> None:
    """A failed drain must be reported, and the descriptors released after it.

    The reporter is the only record an operator has of a discard that failed,
    and the transport close is the only thing that releases the parent-side
    descriptor once no end-of-file is coming. Losing either would leave the
    module's promise — a reclaim that leaves nothing behind is visible — only
    half kept.
    """

    async def run_case() -> None:
        """Release one process whose only stream fails its read."""
        error = _ReadFailureError("the pipe was reset")
        closed: list[bool] = []
        reported: list[tuple[str, BaseException]] = []

        def record(
            _process: object,
            _execution: object,
            *,
            operation: str,
            error: BaseException,
        ) -> None:
            """Record the operation a suppressed failure was attributed to."""
            reported.append((operation, error))

        pipes = types.SimpleNamespace(
            stdin=None,
            stdout=_RaisingReader(error, [], "stdout"),
            stderr=None,
            close_output_transports=lambda: closed.append(True),
        )
        execution = typ.cast("_SubprocessExecution", types.SimpleNamespace())

        with mock.patch.object(reclaim, "_report_reclaim_failure", record):
            await reclaim._release_pipes(
                typ.cast("asyncio.subprocess.Process", pipes),
                execution,
            )

        assert [operation for operation, _ in reported] == ["output_discard"], (
            "a failed read must be reported under the step that owns it"
        )
        assert reported[0][1] is error, (
            "the report must carry the read's own failure, not a replacement"
        )
        assert closed, (
            "the transport close must still run after a failed drain, or the "
            "parent-side descriptor leaks"
        )

    asyncio.run(run_case())
