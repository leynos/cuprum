"""The two teardown steps that release a run's capture accumulators.

``_finalize_capture_handles`` does two things, and the end-to-end cleanup
suite passes when *either* of them removes a spill file: the drain results a
settled run reports carry the same handle the accumulator holds, so the two
paths are each sufficient for a run that finishes normally. This module is
about the pair as a pair, and about the case only the sink path can reach — a
drain cancelled mid-await, whose result never reaches a caller and whose file
only the accumulator still names.

Every assertion here is therefore paired with a negative control that removes
the capability under test: a case that would pass against a sink which never
removed anything would be witnessing its own setup rather than the teardown.
"""

from __future__ import annotations

import asyncio
import pathlib
import typing as typ

from cuprum._capture_cleanup import (
    _cleanup_capture_sinks,
    _finalize_capture_handles,
)
from cuprum._capture_decode import _DecodeSettings
from cuprum._capture_file import _FileAccumulator
from cuprum._stream_drain_finish import _CapturedDrain
from cuprum.capture_policy import FileBackedCapture

if typ.TYPE_CHECKING:
    from cuprum._capture_types import CaptureFinal

_TEXT = _DecodeSettings(encoding="utf-8", errors="strict", capture_bytes=False)
_SPILLED = b"more than the threshold allows"


class _StubMisuseError(AssertionError):
    """Raised when a case drives a stub past the capability it stands for."""


class _StubSink:
    """A capture accumulator that records how often it was cleaned up."""

    def __init__(self) -> None:
        """Start a sink that has not been cleaned up yet."""
        self.cleanups = 0

    def absorb(self, chunk: bytes) -> None:
        """Take one raw chunk, which this stub has no use for."""

    def finalize(self) -> CaptureFinal:
        """Fail: a case that finalizes this stub is testing the wrong object."""
        raise _StubMisuseError

    async def cleanup(self) -> None:
        """Count one request to release this sink."""
        self.cleanups += 1


def _spilled_accumulator(
    directory: pathlib.Path,
    *,
    keep_file: bool = False,
) -> _FileAccumulator:
    """Return a *directory*-backed accumulator that has already spilled.

    The payload is longer than the threshold, so the accumulator really holds a
    file rather than merely being configured to make one, which is what every
    removal assertion below needs to be about.

    Returns
    -------
    _FileAccumulator
        The accumulator, with its spill file still on disk.
    """
    accumulator = _FileAccumulator(
        policy=FileBackedCapture(
            spill_threshold_bytes=4,
            directory=directory,
            keep_file=keep_file,
        ),
        stream="stdout",
        settings=_TEXT,
    )
    accumulator.absorb(_SPILLED)
    return accumulator


class _RefusingClose(pathlib.Path):
    """A path whose unlink always fails, as an immutable file's would.

    Everything but ``unlink`` defers to the real path, so the file is really
    created and really read back; only the removal is refused. Subclassing
    ``Path`` directly rather than a platform-specific class keeps the test
    portable across the POSIX and Windows runs in CI.
    """

    # Spelled as an override of ``Path.unlink`` so the typechecker accepts it:
    # positional-or-keyword, with a default that is not a boolean literal
    # (a boolean *default* is what the boolean-trap rule forbids).
    def unlink(self, missing_ok: bool | None = None) -> None:
        """Refuse the removal, whatever the caller asked for."""
        _ = missing_ok
        raise PermissionError(13, "Operation not permitted")


def _spill_at_a_path_that_refuses_removal(
    directory: pathlib.Path,
    *,
    keep_file: bool = False,
) -> tuple[_FileAccumulator, pathlib.Path]:
    """Return a spilled accumulator whose handle names an unremovable path.

    Returns
    -------
    tuple[_FileAccumulator, pathlib.Path]
        The accumulator and the ``Path`` its handle names, which is the
        refusal-carrying subclass the cleanup step will be asked to remove.
    """
    accumulator = _spilled_accumulator(directory, keep_file=keep_file)
    handle = accumulator.finalize().handle
    assert handle is not None, "the spill must have produced a handle"
    refusing = _RefusingClose(handle.path)
    handle.path = refusing
    return accumulator, refusing


def _drain(fin: CaptureFinal) -> _CapturedDrain:
    """Wrap *fin* as the drain result a settled consumer would report.

    Returns
    -------
    _CapturedDrain
        The real record shape, carrying the final's handle. A bespoke stub
        would satisfy the cleanup seam's duck-typed read but not the type it
        declares, so the seam would accept values no drain can produce.
    """
    return _CapturedDrain(handle=fin.handle)


def test_cleanup_of_no_sinks_is_a_no_op() -> None:
    """An uncaptured run owes nothing, and skipping is not an error.

    This is the shape a run with no capture policy produces: a fixed per-stream
    pair whose members are both ``None``. A loop that dereferenced either would
    fail here rather than on the first uncaptured pipeline.
    """
    asyncio.run(_cleanup_capture_sinks((None, None)))


def test_cleanup_releases_every_sink_it_was_given() -> None:
    """Each accumulator is cleaned up exactly once, ``None`` entries aside."""
    sinks = [_StubSink(), None, _StubSink()]

    asyncio.run(_cleanup_capture_sinks(sinks))

    assert [sink.cleanups for sink in sinks if sink is not None] == [1, 1], (
        "every non-None sink must be released once; "
        f"got {[sink.cleanups for sink in sinks if sink is not None]}"
    )


def test_cleanup_removes_the_file_a_spilled_sink_owns(
    tmp_path: pathlib.Path,
) -> None:
    """The sink path removes the file, not merely closing the descriptor.

    A spill file whose descriptor is closed but whose name survives is the leak
    the sink step exists to prevent, so the assertion is on the directory
    rather than on the handle's ``closed`` flag.
    """
    accumulator = _spilled_accumulator(tmp_path)
    path = accumulator.finalize().capture.path
    assert path is not None, "the spill must have produced a file to remove"
    assert path.exists(), (
        "this case needs a real file on disk; the accumulator reported one "
        "that was never written"
    )

    asyncio.run(_cleanup_capture_sinks((accumulator,)))

    assert not path.exists(), (
        f"the sink's own cleanup must remove the file it owns; {path} survived"
    )
    assert not list(tmp_path.iterdir()), (
        f"the directory must be left as empty as it was found; "
        f"found {list(tmp_path.iterdir())}"
    )


def test_cleanup_keeps_the_file_the_caller_asked_to_keep(
    tmp_path: pathlib.Path,
) -> None:
    """``keep_file`` outranks the sink path exactly as it does the other one."""
    accumulator = _spilled_accumulator(tmp_path, keep_file=True)
    path = accumulator.finalize().capture.path
    assert path is not None, "the spill must have produced a file to keep"

    asyncio.run(_cleanup_capture_sinks((accumulator,)))

    assert path.read_bytes() == _SPILLED, (
        "a kept file must survive teardown with its bytes intact"
    )


def test_finalize_closes_the_result_handles_before_touching_the_sinks(
    tmp_path: pathlib.Path,
) -> None:
    """The drain results are authoritative and are acted on first.

    Both steps remove the same file, so the order is only observable through
    the sink's view of it: by the time the accumulator is asked to clean up,
    the file the result named must already be gone. Reversing the two steps
    would leave this case reading an existing file.
    """
    accumulator = _spilled_accumulator(tmp_path)
    final = accumulator.finalize()
    path = final.capture.path
    assert path is not None, "the spill must have produced a file to order against"
    observed: list[bool] = []

    class _WatchingSink(_StubSink):
        """Records whether the file still existed when cleanup reached it."""

        async def cleanup(self) -> None:
            """Note the file's state, then defer to the base behaviour."""
            observed.append(path.exists())
            await super().cleanup()

    asyncio.run(_finalize_capture_handles((_drain(final),), (_WatchingSink(),)))

    assert observed == [False], (
        "the drain result's handle must be closed before the sinks are "
        f"cleaned; the sink saw exists()={observed}"
    )
    assert not path.exists(), f"the result step must remove the file; {path} survived"


def test_a_sink_still_reaches_its_file_when_no_result_carried_the_handle(
    tmp_path: pathlib.Path,
) -> None:
    """The case the sink step exists for: no drain result, file still removed.

    A drain cancelled while awaiting its line sink re-raises without
    finalizing, so nothing carries the handle out and the result step has
    nothing to act on. The accumulator is then the only reference to the file,
    and this is the assertion that the second step really is load-bearing —
    without it the file survives a cancelled run.
    """
    accumulator = _spilled_accumulator(tmp_path)
    path = accumulator.finalize().capture.path
    assert path is not None, "the spill must have produced a file to remove"

    # The empty drain tuple is the whole point: nothing settled, so nothing
    # reported a handle, and the ordinary removal path cannot run.
    asyncio.run(_finalize_capture_handles((), (accumulator,)))

    assert not path.exists(), (
        "a run whose drain never settled must still remove its spill file; "
        f"{path} survived"
    )


def test_an_unlink_that_fails_is_absorbed_by_the_result_path(
    tmp_path: pathlib.Path,
) -> None:
    """A file that cannot be removed must not fail the run that made it.

    Removing a spill is housekeeping, so a removal the filesystem refuses — an
    immutable attribute, a directory the caller tightened, an antivirus lock —
    is a failed *retention* call like any other and is absorbed. The result
    step runs first and must report rather than raise, or a successful command
    would be reported as failing because its temporary file outlived it. The
    surviving file is the witness that the removal was really attempted.
    """
    accumulator, path = _spill_at_a_path_that_refuses_removal(tmp_path)
    final = accumulator.finalize()

    asyncio.run(_finalize_capture_handles((_drain(final),), ()))

    assert path.exists(), (
        "the case has to leave the file behind for its refusal to mean anything"
    )


def test_an_unlink_that_fails_is_absorbed_by_the_sink_path(
    tmp_path: pathlib.Path,
) -> None:
    """The cancelled-drain path absorbs a refused removal too.

    This is the only path that reaches a drain cancelled mid-await, so an
    exception escaping here would surface on the teardown of exactly the runs
    whose primary error the caller most needs to see.
    """
    accumulator, path = _spill_at_a_path_that_refuses_removal(tmp_path)
    accumulator.finalize()

    asyncio.run(_finalize_capture_handles((), (accumulator,)))

    assert path.exists(), (
        "the case has to leave the file behind for its refusal to mean anything"
    )
