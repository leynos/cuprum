"""A streamed run that stops before end-of-file must release its descriptors.

The exit rendezvous can end a run on a consumer failure, on the run's own
deadline, or on the caller's cancellation — all before the child's output
pipes have necessarily drained. Settling the readers is not enough: a reader
cancelled mid-pipe leaves its parent-side descriptor with the transport that
owns it, and the child spawned here leaves a descendant holding the write ends,
so no end-of-file ever arrives to release them.

Each case therefore drives a real child on both spawn backends and asks the
descriptors themselves whether they were released. The ``wait4`` owner closes
nothing when it reaps, and the fallback's transports close only at end-of-file,
which the descendant prevents: only the run's own release can close these.

The release must also survive a cancellation that interrupts the reconciliation
it is bundled with, which is why one case parks reconciliation at a consumer
that refuses to settle until the test lets it, cancels again while it is
provably in progress, and only then lets the reconciliation finish.
"""

from __future__ import annotations

import asyncio
import dataclasses
import typing as typ

import pytest

from cuprum._pipeline_stream_fds import _extract_stream_fd
from cuprum._stream_drain_state import _RelayDiagnostics
from cuprum._subprocess_stream_exit import _wait_for_streamed_process_exit
from cuprum._subprocess_timeout import _SubprocessTimeoutError
from cuprum._subprocess_wait_types import _RunTaskOwnership
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
    _wait_for_pid_file,
)
from cuprum.unittests._stream_drain_support import captured
from cuprum.unittests._wait4_process_support import _wait4_only

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum._stream_drain_finish import _CapturedDrain

#: Both backends: the descriptor question is answered by the kernel, not by
#: which object owns the pipe, so neither may be left uncovered.
_SPAWN_ROUTES = (
    pytest.param(_spawn_output_pipe_holder, marks=_wait4_only, id="wait4"),
    pytest.param(_spawn_output_pipe_holder_fallback, id="asyncio-fallback"),
)

#: The two ways a run reaches reconciliation before its streams drained: it
#: expired its own deadline, or the caller cancelled the exit wait. Both must
#: release the pipes even if a further cancellation interrupts the settling.
_STOP_ROUTES = (
    pytest.param("deadline", id="deadline-expiry"),
    pytest.param("caller", id="caller-cancellation"),
)

#: Long enough that a run which waited for the child or its descendant could
#: not finish inside a case's own watchdog.
_DESCENDANT_LIFETIME_S = 30.0

#: The child only starts an interpreter and forks, but this host is shared.
_PID_FILE_WAIT_S = 10.0

#: An outer bound per case, so a regression fails rather than hangs the suite.
_WATCHDOG_S = 20.0

#: Short enough to keep the deadline case quick, long enough that the child is
#: certainly still running when it expires.
_TIMEOUT_S = 0.2


class _ConsumerError(RuntimeError):
    """The failure a stream consumer ends the run on."""


async def _failed_consumer() -> _CapturedDrain:
    """Fail as a raising output callback's consumer would."""
    await asyncio.sleep(0)
    raise _ConsumerError


async def _drained_consumer() -> _CapturedDrain:
    """Report an empty capture, as a reader that already drained would."""
    await asyncio.sleep(0)
    return captured("")


async def _parked_consumer() -> _CapturedDrain:
    """Stay parked, as a reader waiting on a pipe that never closes would."""
    await asyncio.sleep(_DESCENDANT_LIFETIME_S)
    return captured("")


async def _interruptible_reconciliation_consumer(
    entered: asyncio.Event,
    release: asyncio.Event,
) -> _CapturedDrain:
    """Park once cancelled, holding reconciliation open until released.

    A cancelled reader normally settles at once, which leaves no window in
    which reconciliation is provably still running. ``entered`` is set from
    inside that window, and the task does not finish until the case releases
    it, so a cancellation arriving in between lands inside the settling.

    Returns
    -------
    _CapturedDrain
        An empty capture, which only an uncancelled reader ever reaches.

    Raises
    ------
    asyncio.CancelledError
        When the reconciliation cancels this reader, after the case releases
        it.
    """
    try:
        await asyncio.sleep(_DESCENDANT_LIFETIME_S)
    except asyncio.CancelledError:
        entered.set()
        await release.wait()
        raise
    return captured("")


def _ownership(
    stdout: cabc.Awaitable[_CapturedDrain],
    stderr: cabc.Awaitable[_CapturedDrain],
) -> _RunTaskOwnership:
    """Assemble the task ownership one case drives the rendezvous with."""
    return _RunTaskOwnership(
        stdin_task=None,
        consumers=(
            asyncio.ensure_future(stdout),
            asyncio.ensure_future(stderr),
        ),
        discard_on_cancel=asyncio.Event(),
        relay_diagnostics=(_RelayDiagnostics(), _RelayDiagnostics()),
    )


def _output_descriptors(
    process: asyncio.subprocess.Process,
) -> tuple[int, ...]:
    """Read both parent-side descriptors, insisting each is watchable."""
    descriptors = tuple(
        _extract_stream_fd(stream) for stream in (process.stdout, process.stderr)
    )
    assert all(fd is not None for fd in descriptors), (
        "each captured stream must have a parent-side descriptor this case can "
        f"watch, got {descriptors!r}"
    )
    return typ.cast("tuple[int, ...]", descriptors)


def _assert_transports_released(descriptors: tuple[int, ...]) -> None:
    """Assert every recorded parent-side descriptor was closed."""
    still_open = [fd for fd in descriptors if _is_fd_open(fd)]
    assert not still_open, (
        f"descriptors {still_open!r} must be released once the run stops before "
        "end-of-file, or the parent-side pipes leak"
    )


def _capture_policy(tmp_path: pathlib.Path) -> FileBackedCapture:
    """Build the capture policy the run bundle under test carries."""
    return FileBackedCapture(spill_threshold_bytes=None, directory=tmp_path)


@_posix_only
@pytest.mark.parametrize("spawn_pipe_holder", _SPAWN_ROUTES)
def test_a_consumer_failure_releases_both_output_descriptors(
    tmp_path: pathlib.Path,
    spawn_pipe_holder: cabc.Callable[
        [pathlib.Path, float],
        cabc.Awaitable[asyncio.subprocess.Process],
    ],
) -> None:
    """The early-failure route must release the pipes the run stopped on.

    The child's descendant keeps both write ends open, so neither pipe can
    reach end-of-file and neither transport closes itself. The consumer failure
    ends the run; if the descriptors are still open afterwards, the failure
    route left them for garbage collection to find.
    """

    async def run_case() -> None:
        """End a real child's run on a consumer failure, then read its fds."""
        pid_file = tmp_path / "descendant.pid"
        process = await spawn_pipe_holder(pid_file, _DESCENDANT_LIFETIME_S)
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = _output_descriptors(process)
            ownership = _ownership(_failed_consumer(), _parked_consumer())

            with pytest.raises(_ConsumerError):
                await asyncio.wait_for(
                    _wait_for_streamed_process_exit(
                        process,
                        _execution(_capture_policy(tmp_path)),
                        ownership,
                        process.pid,
                    ),
                    _WATCHDOG_S,
                )

            assert process.returncode is not None, (
                "the child must be terminated and reaped before the failure "
                "reaches the caller"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the descriptors "
                "are read, or this case proves nothing"
            )
            _assert_transports_released(descriptors)
        finally:
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())


@_posix_only
@pytest.mark.parametrize("spawn_pipe_holder", _SPAWN_ROUTES)
def test_a_deadline_expiry_releases_both_output_descriptors(
    tmp_path: pathlib.Path,
    spawn_pipe_holder: cabc.Callable[
        [pathlib.Path, float],
        cabc.Awaitable[asyncio.subprocess.Process],
    ],
) -> None:
    """The timeout route must release the pipes it stopped reading.

    The route preserves whatever the readers captured for the
    ``TimeoutExpired`` it raises, and that drain is exactly where a close could
    be forgotten: the output has been taken, so nothing further reads the pipe.
    """

    async def run_case() -> None:
        """Expire a real child's run, then read the descriptors it left open."""
        pid_file = tmp_path / "descendant.pid"
        process = await spawn_pipe_holder(pid_file, _DESCENDANT_LIFETIME_S)
        execution = dataclasses.replace(
            _execution(_capture_policy(tmp_path)),
            timeout=_TIMEOUT_S,
        )
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = _output_descriptors(process)
            ownership = _ownership(_drained_consumer(), _drained_consumer())

            with pytest.raises(_SubprocessTimeoutError):
                await asyncio.wait_for(
                    _wait_for_streamed_process_exit(
                        process,
                        execution,
                        ownership,
                        process.pid,
                    ),
                    _WATCHDOG_S,
                )

            assert process.returncode is not None, (
                "the child must be terminated and reaped before the timeout "
                "reaches the caller"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the descriptors "
                "are read, or this case proves nothing"
            )
            _assert_transports_released(descriptors)
        finally:
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())


@_posix_only
@pytest.mark.parametrize("spawn_pipe_holder", _SPAWN_ROUTES)
def test_a_caller_cancellation_releases_both_output_descriptors(
    tmp_path: pathlib.Path,
    spawn_pipe_holder: cabc.Callable[
        [pathlib.Path, float],
        cabc.Awaitable[asyncio.subprocess.Process],
    ],
) -> None:
    """The cancellation route must release the pipes it abandons.

    A cancelled iteration is the ordinary way a line-reading caller stops, and
    it is the route least likely to reach any later cleanup, because control
    returns to the caller with the child already terminated.
    """

    async def run_case() -> None:
        """Cancel a real child's run, then read the descriptors it left open."""
        pid_file = tmp_path / "descendant.pid"
        process = await spawn_pipe_holder(pid_file, _DESCENDANT_LIFETIME_S)
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = _output_descriptors(process)
            ownership = _ownership(_parked_consumer(), _parked_consumer())
            waiting = asyncio.ensure_future(
                _wait_for_streamed_process_exit(
                    process,
                    _execution(_capture_policy(tmp_path)),
                    ownership,
                    process.pid,
                ),
            )

            await asyncio.sleep(_TIMEOUT_S)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, _WATCHDOG_S)

            assert process.returncode is not None, (
                "the child must be terminated and reaped before the "
                "cancellation reaches the caller"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the descriptors "
                "are read, or this case proves nothing"
            )
            _assert_transports_released(descriptors)
        finally:
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())


@_posix_only
@pytest.mark.parametrize("stop_route", _STOP_ROUTES)
@pytest.mark.parametrize("spawn_pipe_holder", _SPAWN_ROUTES)
def test_a_cancellation_during_reconciliation_releases_both_descriptors(
    tmp_path: pathlib.Path,
    spawn_pipe_holder: cabc.Callable[
        [pathlib.Path, float],
        cabc.Awaitable[asyncio.subprocess.Process],
    ],
    stop_route: str,
) -> None:
    """A cancellation interrupting the settling must not skip the release.

    A release awaited after the shielded reconciliation is skipped whenever a
    cancellation lands during that reconciliation, because the shield finishes
    its task and then re-raises. The reconciliation here is held open at a
    consumer that refuses to settle, so the second cancellation is provably
    inside it, and the descriptors are read after the run has ended.
    """

    async def run_case() -> None:
        """Interrupt a real child's settling, then read the descriptors."""
        pid_file = tmp_path / "descendant.pid"
        process = await spawn_pipe_holder(pid_file, _DESCENDANT_LIFETIME_S)
        execution = dataclasses.replace(
            _execution(_capture_policy(tmp_path)),
            timeout=_TIMEOUT_S if stop_route == "deadline" else None,
        )
        entered = asyncio.Event()
        release = asyncio.Event()
        try:
            descendant_pid = await _wait_for_pid_file(pid_file, _PID_FILE_WAIT_S)
            descriptors = _output_descriptors(process)
            ownership = _ownership(
                _interruptible_reconciliation_consumer(entered, release),
                _interruptible_reconciliation_consumer(entered, release),
            )
            waiting = asyncio.ensure_future(
                _wait_for_streamed_process_exit(
                    process,
                    execution,
                    ownership,
                    process.pid,
                ),
            )

            if stop_route == "caller":
                await asyncio.sleep(_TIMEOUT_S)
                waiting.cancel()
            await asyncio.wait_for(entered.wait(), _WATCHDOG_S)
            # The consumers are parked inside reconciliation, so it cannot
            # finish until *release* is set. Cancel once more first, and yield
            # so the cancellation is delivered before the settling resumes:
            # both are queued in call order, so the cancelled task resumes
            # first.
            waiting.cancel()
            await asyncio.sleep(0)
            release.set()

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, _WATCHDOG_S)

            assert process.returncode is not None, (
                "the child must be terminated and reaped before the failure "
                "reaches the caller"
            )
            assert _is_running(descendant_pid), (
                "the descendant must still hold the pipes when the descriptors "
                "are read, or this case proves nothing"
            )
            _assert_transports_released(descriptors)
        finally:
            release.set()
            _kill_descendant(_recorded_pid(pid_file))

    asyncio.run(run_case())
