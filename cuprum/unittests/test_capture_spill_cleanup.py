"""Whether a run removes the spill file it owns, on every way a run can end.

A spill file is the only capture artefact that outlives the process that made
it, so every case here has to establish that there *was* a file before it can
say anything about removal. Two things do that. The obvious one is the record
the run returns, which names the path; the timeout and cancellation routes
return no record at all, so for those a capture hook witnesses the spill
directly, at the transition, by snapshotting the directory the policy chose.

That witness is never vacuous: the hook fires from inside the spill itself, so
a snapshot exists only if the child really crossed the threshold and the
accumulator really wrote a file. A run that stays under its threshold is
covered too, as the negative control, because a witness that fired on every
run would witness nothing.

The invariant is the same on all four routes: a run with ``keep_file=False``
leaves nothing behind in the directory its policy named. ``keep_file=True`` is
the one case that inverts it, and is stated as what the caller receives — a
path that still reads back the bytes.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import TimeoutExpired
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import CapturedStream, FileBackedCapture
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.sh import SafeCmd

# Longer than the spill threshold below, so a run that reaches the child's
# first flush has guaranteed the accumulator something to spill.
_PAYLOAD_BYTES = 512
_THRESHOLD = 8

_SPILL_THEN_EXIT = (
    f"import sys; sys.stdout.write('z' * {_PAYLOAD_BYTES}); "
    "sys.stdout.flush(); sys.exit({code})"
)
_SPILL_THEN_BLOCK = (
    f"import sys, time; sys.stdout.write('z' * {_PAYLOAD_BYTES}); "
    "sys.stdout.flush(); time.sleep(300)"
)
_UNDER_THRESHOLD = "import sys; sys.stdout.write('tiny'); sys.stdout.flush()"

# Generous next to the ~20 ms a bare interpreter takes to start on this host,
# and short enough that the child cannot finish its 300-second sleep: the
# deadline has to fire while the child is still holding the pipe, or the run
# would take the success route and test something else.
_RUN_TIMEOUT_S = 5.0

# How long to wait for the spill witness before giving up on it. The child's
# first flush is the only thing this waits on, which is milliseconds; the cap
# exists so a child that dies without spilling fails the case rather than
# hanging the session.
_WITNESS_TIMEOUT_S = 10.0


class _SpillWitness:
    """A capture hook that records the directory at the moment of a spill.

    Install it with :func:`cuprum.capture_observation.observe_capture` around a
    run to prove the run really created a file, on routes that return no record
    naming one. It holds the *names* present rather than the paths, so a case
    can assert a file appeared without pinning the temporary name.
    """

    def __init__(self, directory: pathlib.Path) -> None:
        """Watch *directory* for files appearing during the run."""
        self.directory = directory
        self.snapshots: list[tuple[str, ...]] = []
        self.categories: list[CaptureEventCategory] = []

    def __call__(self, event: CaptureEvent) -> None:
        """Record a spill's directory listing, and every event's category."""
        self.categories.append(event.category)
        if event.category is CaptureEventCategory.SPILLED_TO_FILE:
            self.snapshots.append(
                tuple(entry.name for entry in self.directory.iterdir())
            )

    def awaited(self) -> tuple[str, ...]:
        """Return the first snapshot, asserting a spill was actually seen."""
        assert self.snapshots, (
            "the run never spilled, so this case would pass because no file "
            f"was ever made; events seen: {self.categories}"
        )
        return self.snapshots[0]


def _builder() -> cabc.Callable[..., SafeCmd]:
    """Return a builder for the current interpreter, allowlisted."""
    return build_python_catalogue_env().builder


def _options(directory: pathlib.Path, *, keep_file: bool = False) -> RunOutputOptions:
    """Capture stdout into *directory*'s spill files, and stderr not at all.

    stderr is left uncaptured so the record tuple holds the stdout entry alone,
    which keeps ``captures[0]`` the spill's record rather than one a policy
    choice could reorder.

    Returns
    -------
    RunOutputOptions
        Output options whose stdout policy spills into *directory*.
    """
    return RunOutputOptions(
        capture_stdout=FileBackedCapture(
            spill_threshold_bytes=_THRESHOLD,
            directory=directory,
            keep_file=keep_file,
        ),
        capture_stderr=False,
    )


def _only_spill(captures: tuple[CapturedStream, ...]) -> CapturedStream:
    """Return the single capture record, asserting there is exactly one."""
    assert len(captures) == 1, (
        f"this case expects one captured stream, set by _options; got {captures!r}"
    )
    return captures[0]


def _reported_path(record: CapturedStream) -> pathlib.Path:
    """Return the path *record* names, asserting it names one at all."""
    assert record.storage == "file", (
        f"the record must report the stream as spilled; got {record.storage!r}"
    )
    assert record.path is not None, "a spilled record must name its file"
    return record.path


def _directory_is_clean(directory: pathlib.Path, *, outcome: str) -> None:
    """Assert *outcome* left no spill file behind."""
    remaining = list(directory.iterdir())
    assert remaining == [], (
        f"{outcome} must leave no spill file behind; found {remaining}"
    )


def test_a_successful_run_removes_its_spill_file_before_returning_the_result(
    tmp_path: pathlib.Path,
) -> None:
    """A completed run hands back a record for a file it has already removed.

    This is the lifetime the implementation settles on: the path a caller
    receives is a locator for what was captured, not a claim that the file is
    still there, and a long-lived process collecting results accumulates no
    files with them. The witness pins that the file existed, because a run
    whose spill had silently failed would satisfy the removal assertion for
    the wrong reason.
    """
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness):
        result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path)
        )
    witnessed = witness.awaited()

    record = _only_spill(result.captures)
    path = _reported_path(record)
    assert path.name in witnessed, (
        f"the witness saw {witnessed} but the record names {path.name}"
    )
    assert not path.exists(), (
        f"the run must remove the file it reported at {path} before returning"
    )
    assert record.owned is True, (
        "the record described the file as owned, so this run was the one meant "
        "to remove it"
    )
    _directory_is_clean(tmp_path, outcome="a successful run")


@pytest.mark.parametrize("exit_code", [1, 3, 127])
def test_a_non_zero_exit_still_removes_the_spill_file(
    tmp_path: pathlib.Path,
    exit_code: int,
) -> None:
    """A failing child's exit status is no reason to leak the parent's disk.

    Nothing in the cleanup path may branch on the exit code, so the run that
    failed must be exactly as tidy as the one that succeeded. The code is
    carried through the message rather than inspected, which is what keeps
    each row a statement about the same invariant rather than three cases
    pretending to differ.
    """
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness):
        result = _builder()("-c", _SPILL_THEN_EXIT.format(code=exit_code)).run_sync(
            output=_options(tmp_path)
        )
    witnessed = witness.awaited()

    assert result.exit_code == exit_code, "the child's status must reach the caller"
    path = _reported_path(_only_spill(result.captures))
    assert path.name in witnessed, (
        f"the witness saw {witnessed} but the record names {path.name}"
    )
    assert not path.exists(), (
        f"exit status {exit_code} must not leave its spill file behind"
    )
    _directory_is_clean(tmp_path, outcome=f"a run exiting with {exit_code}")


def test_a_timed_out_run_removes_the_spill_file_it_never_reported(
    tmp_path: pathlib.Path,
) -> None:
    """The deadline route removes the file it has no record left to carry.

    A timeout raises rather than returning, so nothing hands the path to the
    caller and the run's own teardown is the only thing that can unlink it.
    That makes this the route where the cleanup obligation on the accumulator
    is load-bearing rather than merely consistent, and the spill is witnessed
    through the capture hook because no result exists to name it.
    """
    witness = _SpillWitness(tmp_path)

    with pytest.raises(TimeoutExpired) as expired, observe_capture(witness):
        _builder()("-c", _SPILL_THEN_BLOCK).run_sync(
            timeout=_RUN_TIMEOUT_S,
            output=_options(tmp_path),
        )

    witnessed = witness.awaited()
    assert witnessed, "the spill must have been seen before the deadline fired"
    assert expired.value.output is None, (
        "text mode reports a spilled capture as absent, not as text"
    )
    _directory_is_clean(tmp_path, outcome="a timed-out run")


def test_an_externally_cancelled_run_removes_the_spill_file(
    tmp_path: pathlib.Path,
) -> None:
    """Cancellation is not an excuse to leave the parent's file behind.

    A caller cancelling mid-run unwinds through the same reconciliation every
    other route uses, so the file goes with it. The cancellation is issued only
    once the spill has been witnessed, so the case cannot pass by cancelling a
    run that had not created anything yet.
    """

    async def cancelled_run() -> None:
        """Spill, wait for the witness, then cancel the run from outside."""
        witness = _SpillWitness(tmp_path)
        with observe_capture(witness):
            run = asyncio.create_task(
                _builder()("-c", _SPILL_THEN_BLOCK).run(
                    timeout=300,
                    output=_options(tmp_path),
                )
            )
            await _await_witness(witness)
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run

    asyncio.run(cancelled_run())

    _directory_is_clean(tmp_path, outcome="a cancelled run")


async def _await_witness(witness: _SpillWitness) -> None:
    """Fail unless *witness* sees a spill before the give-up deadline.

    The readiness signal is a file another process wrote, which no asyncio
    primitive can observe, so polling is the coordination — and polling a
    directory is the one thing this module cannot avoid.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _WITNESS_TIMEOUT_S
    while loop.time() < deadline:
        if witness.snapshots:
            return
        await asyncio.sleep(0.01)
    pytest.fail(
        f"the child never spilled before {_WITNESS_TIMEOUT_S}s; "
        f"events seen: {witness.categories}"
    )


def test_a_stream_under_its_threshold_is_not_a_witnessed_spill(
    tmp_path: pathlib.Path,
) -> None:
    """The negative control: no crossing, no spill, and therefore no witness.

    Without this the module could not claim its witness discriminates. A run
    that never reaches the threshold must leave the directory empty *and* fire
    no spill event, so 'nothing there afterwards' is a statement about a spill
    that happened rather than about one that never did.
    """
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness):
        result = _builder()("-c", _UNDER_THRESHOLD).run_sync(output=_options(tmp_path))

    assert witness.snapshots == [], (
        f"a stream within its threshold must not spill; got {witness.snapshots!r}"
    )
    assert witness.categories == [], (
        "a memory-backed capture must emit no storage event; "
        f"got {witness.categories!r}"
    )
    record = _only_spill(result.captures)
    assert record.storage == "memory", "no spill means the capture stayed on the heap"
    assert record.total_bytes == len("tiny")
    _directory_is_clean(tmp_path, outcome="a run under its spill threshold")


def test_a_kept_spill_file_survives_completed_runs_whole(
    tmp_path: pathlib.Path,
) -> None:
    """``keep_file`` hands the file over instead of removing it.

    The caller asked for the file, so the run's obligation shrinks to closing
    it. The bytes are asserted as well as the path: a run that removed nothing
    but truncated what it wrote would pass an existence check alone.
    """
    witness = _SpillWitness(tmp_path)
    with observe_capture(witness):
        result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path, keep_file=True)
        )
    witness.awaited()

    record = _only_spill(result.captures)
    path = _reported_path(record)
    assert path.exists(), "a kept spill file must outlive the run that made it"
    assert path.read_bytes() == b"z" * _PAYLOAD_BYTES, (
        "a kept file must hold the child's whole stream; "
        f"got {len(path.read_bytes())} of {_PAYLOAD_BYTES} bytes"
    )
    assert record.owned is False, (
        "a handed-over file reports unowned: this run is not the one to remove it"
    )


def test_a_kept_spill_file_survives_a_timed_out_run_whole(
    tmp_path: pathlib.Path,
) -> None:
    """The keep exemption holds on the raising route, not just the returning one.

    The file still has to be complete, and the timeout path is where the run
    has the least state left to reason about: it tears down the child, the
    readers, and the group, and must still stop short of the file it was told
    to hand over.
    """
    witness = _SpillWitness(tmp_path)

    with pytest.raises(TimeoutExpired), observe_capture(witness):
        _builder()("-c", _SPILL_THEN_BLOCK).run_sync(
            timeout=_RUN_TIMEOUT_S,
            output=_options(tmp_path, keep_file=True),
        )

    witnessed = witness.awaited()
    assert witnessed, "the spill must have been seen before the deadline fired"
    (name,) = witnessed
    path = tmp_path / name
    assert path.exists(), "a kept spill file must survive the timeout's teardown"
    assert path.read_bytes() == b"z" * _PAYLOAD_BYTES, (
        "a kept file must hold the flushed prefix in full; "
        f"got {len(path.read_bytes())} bytes"
    )


def test_two_runs_into_one_directory_leave_nothing_behind(
    tmp_path: pathlib.Path,
) -> None:
    """Each run owns its own file, and neither this run's cleanup nor the other's.

    The handle is per-capture state, so two runs sharing a spill directory must
    not let one run's cleanup reach across to the other's file — and after both
    have finished the directory must be empty either way. This is the case that
    would catch a cleanup implemented as 'remove everything in the directory',
    which would pass every single-run test above.
    """
    first = _SpillWitness(tmp_path)
    with observe_capture(first):
        first_result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path)
        )
    first_path = _reported_path(_only_spill(first_result.captures))

    # Between the runs exactly one file exists: the second run's spill has not
    # happened yet, so this is also the assertion that the first run removed
    # its own file rather than the second run removing both.
    assert list(tmp_path.iterdir()) == [], (
        f"the first run must remove its file before the second starts; {first_path}"
    )

    second = _SpillWitness(tmp_path)
    with observe_capture(second):
        second_result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path)
        )
    second_path = _reported_path(_only_spill(second_result.captures))

    assert second_path != first_path, "two spills must not share a file"
    assert second_path.name in second.awaited()
    _directory_is_clean(tmp_path, outcome="two runs sharing a spill directory")
