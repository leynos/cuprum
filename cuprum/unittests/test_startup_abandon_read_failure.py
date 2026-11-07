"""A failed output read during startup abandonment must not be lost.

The reclaim's discard step reads both output streams to end-of-file under one
grace. A read can fail in its own right — a reset pipe is the ordinary case —
and the module's contract is that such a failure reaches an operator through
the same ``output_discard`` report the reclaim uses for a drain that timed out.
``asyncio.gather(..., return_exceptions=True)`` deliberately keeps one stream's
failure from cancelling the other, so the failure has to be collected and
re-raised rather than left in the results: dropped, it would leave only the
transport close behind it and no record of why that close was needed.

A failure that completes just before the shared grace does not lose to it: the
discard raises the failure it already holds rather than report only an expiry,
and the transport close still runs for the stream that never settled.

Read stubs are used for the discard's own contract, because the question there
is what the discard does with a failure, not how a pipe comes to fail. The
descriptor case is different: it drives a real child on both backends, because
"the transports were released" is a fact about descriptors that only a real
pipe can witness.
"""

from __future__ import annotations

import asyncio
import types
import typing as typ
from unittest import mock

import pytest

from cuprum import _subprocess_startup_abandon as reclaim
from cuprum._pipeline_stream_fds import _extract_stream_fd, _stream_transport
from cuprum._subprocess_startup_abandon import _discard_outputs
from cuprum.capture_policy import FileBackedCapture
from cuprum.unittests._startup_abandon_support import (
    _execution,
    _is_fd_open,
    _is_running,
    _kill_descendant,
    _posix_only,
    _recorded_pid,
    _spawn_output_pipe_holder,
    _spawn_output_pipe_holder_fallback,
    _StalledReader,
    _wait_for_pid_file,
)
from cuprum.unittests._wait4_process_support import _wait4_only

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

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
        reporter = _RecordingReporter()
        pipes = types.SimpleNamespace(
            stdin=None,
            stdout=_RaisingReader(error, [], "stdout"),
            stderr=None,
            close_output_transports=lambda: closed.append(True),
        )
        execution = typ.cast("_SubprocessExecution", types.SimpleNamespace())

        with mock.patch.object(reclaim, "_report_reclaim_failure", reporter):
            await reclaim._release_pipes(
                typ.cast("asyncio.subprocess.Process", pipes),
                execution,
            )

        assert reporter.operations == ["output_discard"], (
            "a failed read must be reported under the step that owns it"
        )
        assert reporter.reports[0][1] is error, (
            "the report must carry the read's own failure, not a replacement"
        )
        assert closed, (
            "the transport close must still run after a failed drain, or the "
            "parent-side descriptor leaks"
        )

    asyncio.run(run_case())


def test_a_failed_read_is_reported_when_the_other_read_stalls() -> None:
    """A failure the grace outlived must still be the reported one.

    One stream fails at once and its neighbour never returns, so the shared
    grace expires while the failure is already in hand. Reporting the pair as a
    plain expiry would drop that failure and leave an operator knowing only
    that the discard timed out; the transport close must still run, because no
    end-of-file is coming for the stalled pipe either.
    """

    async def run_case() -> None:
        """Release one process whose streams fail and stall."""
        error = _ReadFailureError("the pipe was reset")
        entered: list[str] = []
        closed: list[bool] = []
        reporter = _RecordingReporter()
        pipes = types.SimpleNamespace(
            stdin=None,
            stdout=_RaisingReader(error, [], "stdout"),
            stderr=_StalledReader(entered, "stderr"),
            close_output_transports=lambda: closed.append(True),
        )
        execution = typ.cast("_SubprocessExecution", types.SimpleNamespace())

        with mock.patch.object(reclaim, "_report_reclaim_failure", reporter):
            await reclaim._release_pipes(
                typ.cast("asyncio.subprocess.Process", pipes),
                execution,
            )

        assert entered == ["stderr"], (
            "the stalled read must be entered inside the same grace as the "
            "failure, or this case says nothing about a shared window"
        )
        assert reporter.operations == ["output_discard"], (
            "the discard must report the step that owned both reads, got "
            f"{reporter.reports!r}"
        )
        assert reporter.reports[0][1] is error, (
            "the completed read failure must be the reported error, not the "
            "expiry that outlived it"
        )
        assert closed, (
            "the transport close must still run with a stalled neighbour, or "
            "the parent-side descriptor leaks"
        )

    asyncio.run(run_case())


#: The two ways a run obtains its child; both must release the parent's
#: descriptors after a failed read, and neither releases them by itself.
_SPAWN_ROUTES = (
    pytest.param(_spawn_output_pipe_holder, marks=_wait4_only, id="wait4"),
    pytest.param(_spawn_output_pipe_holder_fallback, id="asyncio-fallback"),
)

#: Long enough that a reclaim which waited for the child or its descendant to
#: exit could not finish inside the case's own bound.
_DESCENDANT_LIFETIME_S = 30.0

#: The child only starts an interpreter and forks, but this host is shared.
_PID_FILE_WAIT_S = 10.0


class _FailingStream:
    """A stream whose read fails, still publishing the transport it wraps.

    The transport is kept reachable under the private name
    :func:`~cuprum._pipeline_stream_fds._stream_transport` consults, so the
    fallback's per-stream close still names the pipe the real reader owned.
    A stub that hid the transport would let this case pass while the real
    descriptor leaked.
    """

    def __init__(self, transport: object, error: BaseException) -> None:
        """Bind the transport to publish and the failure to raise."""
        self._transport = transport
        self._error = error

    async def read(self, _size: int) -> bytes:
        """Fail as a reset pipe would, without consuming anything."""
        raise self._error


class _EofStream:
    """A stream that reports end-of-file at once, publishing its transport.

    The failure under test is the other stream's; this one settles inside the
    shared grace so the discard reaches its ``gather`` and re-raises that
    failure, rather than expiring on a pipe the descendant still holds.
    """

    def __init__(self, transport: object) -> None:
        """Bind the transport to publish."""
        self._transport = transport

    async def read(self, _size: int) -> bytes:
        """Report end-of-file, as a drained pipe does."""
        return b""


class _RecordingReporter:
    """Stand in for the reclaim's reporter, keeping what it was told.

    The reclaim reports through a module-level function, so the stand-in has to
    be a callable object rather than a closure: the case wants one record that
    every report appends to, and a class keeps that list out of the case body.
    """

    def __init__(self) -> None:
        """Start with nothing reported."""
        self.reports: list[tuple[str, BaseException]] = []

    def __call__(
        self,
        _process: object,
        _execution: object,
        *,
        operation: str,
        error: BaseException,
    ) -> None:
        """Record the operation a suppressed failure was attributed to."""
        self.reports.append((operation, error))

    @property
    def operations(self) -> list[str]:
        """The reported operations, in the order they arrived."""
        return [operation for operation, _ in self.reports]


def _output_descriptors(
    process: asyncio.subprocess.Process,
) -> tuple[int | None, ...]:
    """Read both parent-side pipe descriptors, insisting each is watchable.

    Returns
    -------
    tuple[int | None, ...]
        The descriptors behind the child's two captured streams.
    """
    descriptors = tuple(
        _extract_stream_fd(stream) for stream in (process.stdout, process.stderr)
    )
    assert all(fd is not None for fd in descriptors), (
        "each captured stream must have a parent-side pipe whose descriptor "
        "this case can watch"
    )
    return descriptors


def _replace_readers(
    process: asyncio.subprocess.Process,
    error: BaseException,
) -> None:
    """Fail one stream's read and let the other report end-of-file.

    Both stubs publish the transport the real reader held, so a fallback close
    that follows the stream still names the pipe under test.
    """
    process.stdout = typ.cast(
        "asyncio.StreamReader",
        _FailingStream(_stream_transport(process.stdout), error),
    )
    process.stderr = typ.cast(
        "asyncio.StreamReader",
        _EofStream(_stream_transport(process.stderr)),
    )


def _assert_both_descriptors_closed(descriptors: tuple[int | None, ...]) -> None:
    """Assert every recorded parent-side descriptor was released."""
    for fd in descriptors:
        assert fd is not None, "each captured stream's descriptor must be watchable"
        assert not _is_fd_open(fd), (
            f"descriptor {fd} must be closed after a failed read, or the "
            "parent-side pipe leaks"
        )


@_posix_only
@pytest.mark.parametrize("spawn_pipe_holder", _SPAWN_ROUTES)
def test_a_failed_read_still_releases_both_output_descriptors(
    tmp_path: pathlib.Path,
    spawn_pipe_holder: cabc.Callable[
        [pathlib.Path, float],
        cabc.Awaitable[asyncio.subprocess.Process],
    ],
) -> None:
    """A failed discard must release both pipes, not only the one that failed.

    The reclaim reports a read failure and then asks for the transports to be
    released, so a case that only watched the report could pass while the
    other stream's descriptor stayed open. Both parent-side descriptors are
    read here, on both backends, from a child a descendant keeps alive: the
    ``wait4`` owner reaps without closing anything, and the fallback's
    transports close only at end-of-file, so only the reclaim's close can
    release them. The failing read is the one the report carries, which keeps
    this on the read-failure path rather than the expired-grace one.
    """

    async def run_case() -> None:
        """Abandon a real child after one of its two reads fails."""
        pid_file = tmp_path / "descendant.pid"
        process = await spawn_pipe_holder(pid_file, _DESCENDANT_LIFETIME_S)
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = _output_descriptors(process)
            error = _ReadFailureError("the pipe was reset")
            _replace_readers(process, error)
            reporter = _RecordingReporter()

            with mock.patch.object(reclaim, "_report_reclaim_failure", reporter):
                await reclaim._abandon_started_process(
                    process,
                    _execution(
                        FileBackedCapture(
                            spill_threshold_bytes=None,
                            directory=tmp_path,
                        ),
                    ),
                )

            assert process.returncode is not None, (
                "the child must still be reaped after a failed read"
            )
            assert reporter.operations == ["output_discard"], (
                f"the read failure must be the reported one, got {reporter.reports!r}"
            )
            assert reporter.reports[0][1] is error, (
                "the report must carry the read's own failure"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the descriptors "
                "are read, or this case proves nothing"
            )
            _assert_both_descriptors_closed(descriptors)
        finally:
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())
