"""What a capture policy reports when a timeout cuts the child short (#443).

A timeout raises rather than returning, and the child is still holding the pipe
when it does, so the bytes it had already sent are the whole of what is
available: there is no later chunk and no clean EOF to finalize against. Every
case here therefore asserts the *prefix* the child flushed before it stalled,
not merely that something was reported — a run that discarded its retention on
the raising route would leave `None` behind, and a run whose bound had silently
eaten the prefix would leave an empty or shortened value.

The file-backed cases have no record to read the path from, because the reroute
returns nothing, so they locate the file the only way a caller can: by reading
the directory the policy named.
"""

from __future__ import annotations

import typing as typ

import pytest

from cuprum import TimeoutExpired
from cuprum.capture_events import CaptureEvent, CaptureEventCategory
from cuprum.capture_observation import observe_capture
from cuprum.capture_policy import BoundedCapture, FileBackedCapture
from cuprum.sh import RunOutputOptions
from tests.helpers.catalogue import build_python_catalogue_env

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import pathlib

    from cuprum.sh import SafeCmd

# The child writes this, flushes it, and only then stops responding. Its length
# is the value every assertion below compares against, and the bound cases are
# chosen either side of it so that one retains the prefix whole and the other
# retains a genuine tail of it.
_PREFIX = "abcdefghij"
_PREFIX_LEN = len(_PREFIX)
_TAIL_BOUND = 4
_TAIL = _PREFIX[-_TAIL_BOUND:]
_DROPPED = _PREFIX_LEN - _TAIL_BOUND

# Far longer than the deadline, so the timeout always fires while the child is
# mid-sleep rather than racing it to exit. Generous next to the ~20 ms a bare
# interpreter takes to start on this host.
_STALL_SECONDS = 300
_TIMEOUT_S = 5.0

# Written as one source string per stream so a case can say which pipe carries
# the prefix, rather than every case sharing a child that fills both.
_STDOUT_STALL = (
    f"import sys, time; sys.stdout.write({_PREFIX!r}); "
    f"sys.stdout.flush(); time.sleep({_STALL_SECONDS})"
)
_STDERR_STALL = (
    f"import sys, time; sys.stderr.write({_PREFIX!r}); "
    f"sys.stderr.flush(); time.sleep({_STALL_SECONDS})"
)


def _builder() -> cabc.Callable[..., SafeCmd]:
    """Return a builder for the current interpreter, allowlisted.

    A *builder*, not a command: the catalogue hands back the callable and each
    case supplies its own ``-c`` source, so annotating this ``SafeCmd`` would
    describe a value the caller immediately calls.

    Returns
    -------
    collections.abc.Callable[..., SafeCmd]
        A builder that constructs commands for the current interpreter.
    """
    return build_python_catalogue_env().builder


def _run_and_capture_failure(source: str, options: RunOutputOptions) -> TimeoutExpired:
    """Run *source* under *options*, asserting it times out and returning it.

    A run that completed would take the returning route instead of the raising
    one, so ``pytest.raises`` failing here is the case refusing to test the
    wrong path — and the child's stall is longer than any deadline this module
    sets, so reaching it means the deadline did not fire at all.

    Returns
    -------
    TimeoutExpired
        The failure, carrying whatever the policies retained before the stall.
    """
    with pytest.raises(TimeoutExpired) as expired:
        _builder()("-c", source).run_sync(timeout=_TIMEOUT_S, output=options)
    return expired.value


def _sole_spill_file(directory: pathlib.Path) -> pathlib.Path:
    """Return the one file in *directory*, asserting there is exactly one.

    A timed-out run hands back no result, so this is how a case reaches the
    spill the policy created. Asserting the count rather than taking the first
    entry means an empty directory — a spill that never happened — fails here
    instead of turning the byte comparison below into a comparison of nothing.

    Returns
    -------
    pathlib.Path
        The single file the spill left behind.
    """
    entries = sorted(directory.iterdir())
    assert len(entries) == 1, (
        f"the spilled run must leave exactly its own file in {directory}; "
        f"found {entries}"
    )
    return entries[0]


def test_a_spilled_capture_survives_a_timeout_as_a_readable_file(
    tmp_path: pathlib.Path,
) -> None:
    """The file holds every byte the child sent before the deadline fired.

    A caller who keeps the file keeps it on the raising route too, so this is
    the case that says the retention outlives the failure. Nothing about the
    bytes is inferred from the file's existence: the whole prefix is compared.
    """
    failure = _run_and_capture_failure(
        _STDOUT_STALL,
        RunOutputOptions(
            capture_stdout=FileBackedCapture(
                spill_threshold_bytes=_TAIL_BOUND,
                directory=tmp_path,
                keep_file=True,
            ),
            capture_stderr=False,
        ),
    )

    assert failure.output is None, (
        "a file-backed capture reports no text on the result, so the payload "
        "must be absent rather than a reading of the file"
    )
    path = _sole_spill_file(tmp_path)
    assert path.read_bytes() == _PREFIX.encode(), (
        "the kept file must hold the child's flushed prefix in full; "
        f"got {path.read_bytes()!r} rather than {_PREFIX!r}"
    )


@pytest.mark.parametrize(
    ("max_bytes", "expected"),
    [
        pytest.param(64, _PREFIX, id="bound-above-the-prefix-keeps-it-whole"),
        pytest.param(_TAIL_BOUND, _TAIL, id="bound-below-the-prefix-keeps-a-tail"),
    ],
)
def test_a_bounded_capture_reports_what_arrived_before_the_stall(
    max_bytes: int,
    expected: str,
) -> None:
    """A bounded stream is finalized on the raising route, not only at EOF.

    The two rows straddle the prefix's length, so between them they pin the
    behaviour at both ends: a bound the child never reached must report the
    prefix entire, and a bound it exceeded must report exactly the tail the
    policy promises. A finalizer that ran only on the clean-EOF path would
    return ``None`` for both rows.
    """
    failure = _run_and_capture_failure(
        _STDOUT_STALL,
        RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=max_bytes),
            capture_stderr=False,
        ),
    )

    assert failure.output == expected, (
        f"a bound of {max_bytes} on the {_PREFIX_LEN}-byte prefix must report "
        f"{expected!r}; got {failure.output!r}"
    )


def test_a_bounded_capture_reports_the_bytes_it_dropped_at_the_stall() -> None:
    """The drop count is exact on the raising route, not merely non-zero.

    The count is what tells a caller how much of the child's output the result
    does not show, and a run that lost its bookkeeping to the timeout would
    report a tail with no way to know what preceded it.
    """
    events: list[CaptureEvent] = []
    with observe_capture(events.append):
        failure = _run_and_capture_failure(
            _STDOUT_STALL,
            RunOutputOptions(
                capture_stdout=BoundedCapture(max_bytes=_TAIL_BOUND),
                capture_stderr=False,
            ),
        )

    assert failure.output == _TAIL, "the retained tail must reach the failure"
    (truncation,) = [
        event for event in events if event.category is CaptureEventCategory.TRUNCATED
    ]
    assert truncation.total_bytes == _PREFIX_LEN, (
        "the event must report the child's whole output, not the retained tail; "
        f"got {truncation.total_bytes} of {_PREFIX_LEN}"
    )
    assert truncation.dropped_bytes == _DROPPED, (
        f"a bound of {_TAIL_BOUND} on {_PREFIX_LEN} bytes drops {_DROPPED}; "
        f"got {truncation.dropped_bytes}"
    )


def test_stderr_recovers_its_own_prefix_when_the_child_stalls() -> None:
    """The two streams fail together but are retained apart.

    Only stderr carries the prefix here, so a run that reported stdout's
    (correctly absent) payload for both would fail this case, and so would one
    that resolved the two streams from a single policy. This is the timeout's
    half of the per-stream independence requirement.
    """
    failure = _run_and_capture_failure(
        _STDERR_STALL,
        RunOutputOptions(
            capture_stdout=BoundedCapture(max_bytes=64),
            capture_stderr=BoundedCapture(max_bytes=64),
        ),
    )

    assert failure.stderr == _PREFIX, (
        f"stderr must report the prefix it received; got {failure.stderr!r}"
    )
    assert failure.output == "", (
        "stdout received nothing before the stall, so its bounded capture "
        f"must report an empty string rather than the other stream's bytes; "
        f"got {failure.output!r}"
    )
