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
leaves nothing behind in the directory its policy named. Kept-file outcomes
are exercised separately, including success, timeout, and cancellation.
"""

from __future__ import annotations

import asyncio
import typing as typ

import pytest

from cuprum import TimeoutExpired
from cuprum.capture_observation import observe_capture
from cuprum.unittests._capture_spill_run_support import (
    _RUN_TIMEOUT_S,
    _SPILL_THEN_BLOCK,
    _SPILL_THEN_EXIT,
    _UNDER_THRESHOLD,
    _await_witness,
    _builder,
    _directory_is_clean,
    _only_spill,
    _options,
    _reported_path,
)
from cuprum.unittests._capture_witness_support import _SpillWitness

if typ.TYPE_CHECKING:
    import pathlib


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

    # The observer is entered first so the window it watches covers the whole
    # time the child runs, not merely the time after the deadline has already
    # fired. The deadline's own exception is still what ends the block.
    with observe_capture(witness), pytest.raises(TimeoutExpired) as expired:
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

    assert not witness.snapshots, (
        f"a stream within its threshold must not spill; got {witness.snapshots!r}"
    )
    assert not witness.categories, (
        "a memory-backed capture must emit no storage event; "
        f"got {witness.categories!r}"
    )
    record = _only_spill(result.captures)
    assert record.storage == "memory", "no spill means the capture stayed on the heap"
    assert record.total_bytes == len("tiny"), (
        "the captured byte count must match the child output"
    )
    _directory_is_clean(tmp_path, outcome="a run under its spill threshold")


def test_two_runs_into_one_directory_leave_nothing_behind(
    tmp_path: pathlib.Path,
) -> None:
    """Each run removes its own spill file and leaves every other file alone.

    The handle is per-capture state, so two runs sharing a spill directory must
    not let one run's cleanup reach across to the other's file — and after both
    have finished the directory must be empty either way. The bystander file is
    what makes this case catch a cleanup implemented as 'remove everything in
    the directory', which an empty-directory check alone cannot see and which
    would pass every single-run test above.
    """
    bystander = tmp_path / "not-a-spill"
    bystander.write_bytes(b"keep me")

    first = _SpillWitness(tmp_path)
    with observe_capture(first):
        first_result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path)
        )
    first_path = _reported_path(_only_spill(first_result.captures))

    # Between the runs only the bystander exists: the second run's spill has
    # not happened yet, so this assertion confirms the first run removed its
    # own file rather than the second run removing both.
    assert list(tmp_path.iterdir()) == [bystander], (
        f"the first run must remove its file before the second starts; {first_path}"
    )

    second = _SpillWitness(tmp_path)
    with observe_capture(second):
        second_result = _builder()("-c", _SPILL_THEN_EXIT.format(code=0)).run_sync(
            output=_options(tmp_path)
        )
    second_path = _reported_path(_only_spill(second_result.captures))

    assert second_path != first_path, "two spills must not share a file"
    assert second_path.name in second.awaited(), (
        f"the witness must have seen the second run's file {second_path.name}"
    )
    assert bystander.read_bytes() == b"keep me", (
        "a run's cleanup must not reach a file it did not create"
    )
    bystander.unlink()
    _directory_is_clean(tmp_path, outcome="two runs sharing a spill directory")
