"""Injected-failure contracts for the Linux process-state diagnostic helpers.

The healthy-host contracts live in
:mod:`tests.test_process_state_helper`; this module drives the *degradations*
that module documents -- the paths the helpers take when procfs cannot answer.
None of them is reachable on a working Linux host, and each reports "no
evidence" through a value a caller acts on, so an untested one is a stall rule
that silently stops firing rather than an error anyone notices.

Every fault is injected at a named seam -- one path, one descriptor, one
module -- rather than by call order, so an incidental read of any other file
during a test still sees real behaviour and a pass cannot come from breaking
more than the test intended.
"""

from __future__ import annotations

import os
import pathlib
import sys
import typing as typ

from tests.helpers import process_state as state_helpers
from tests.helpers.process_state import (
    _pending_bytes,
    _state_from_stat,
    child_pipes,
    parent_read_fds,
    parent_write_fds,
    process_state,
    read_end_pending_bytes,
)

if typ.TYPE_CHECKING:
    import pytest

# The degradations below have no host state that produces them on demand: a
# procfs that cannot be consulted, a stat file that exists but cannot be
# read, a descriptor entry that vanishes mid-listing, an ioctl the kernel
# refuses, and a runtime without ``fcntl``.


def _missing_procfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the helpers take the no-procfs path, as an unsupported host does."""
    monkeypatch.setattr(state_helpers, "_procfs_available", lambda: False)


def _fail_path_method(
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    target: str,
    error: OSError,
) -> None:
    """Make one ``Path`` method raise ``error`` for ``target`` alone.

    One seam serves three subjects -- a stat file, a descriptor directory, and
    individual descriptor links -- because each fault has to reach exactly one
    path. Matching by path rather than by call order keeps every other file
    answering normally, so a pass cannot come from breaking more than the test
    intended.
    """
    original = getattr(pathlib.Path, method_name)

    def failing(self: pathlib.Path, *args: object, **kwargs: object) -> object:
        if str(self) == target:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, method_name, failing)


def _fail_ioctl(monkeypatch: pytest.MonkeyPatch, error: OSError) -> None:
    """Make the ``FIONREAD`` ioctl raise ``error``, leaving its imports intact."""
    import fcntl

    def failing(*args: object, **kwargs: object) -> bytes:
        raise error

    monkeypatch.setattr(fcntl, "ioctl", failing)


def _hide_fcntl_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import fcntl`` fail, as it does on a host without the module."""
    monkeypatch.setitem(sys.modules, "fcntl", None)


def test_the_procfs_guard_reports_no_evidence_on_an_unsupported_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without procfs, every probe reports no evidence rather than raising.

    One guard, four entry points: the process record goes unavailable, and the
    three descriptor probes each answer with an empty tuple. A rule reading
    "no parent writer" cannot fire on this answer, which is the documented
    intent -- the distinction between "unobservable" and "nothing there" is
    deliberately not carried.
    """
    _missing_procfs(monkeypatch)
    pid = os.getpid()

    state = process_state(pid)
    assert not state.available, (
        f"a host without procfs must report an unavailable record, got {state!r}"
    )
    assert not state.exited, (
        "an unavailable record must not claim the process has exited; that is a "
        f"different answer, got {state!r}"
    )
    assert not child_pipes(pid), (
        f"without procfs there are no child pipes to report, got {child_pipes(pid)!r}"
    )
    assert not parent_write_fds("pipe:[1]"), (
        "without procfs no parent writer can be identified"
    )
    assert not parent_read_fds("pipe:[1]"), (
        "without procfs no parent read end can be identified"
    )


def test_a_stat_read_that_fails_leaves_the_process_record_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A readable-but-failing ``stat`` is unobservable, not an exited process.

    The two outcomes are the module's central distinction and they travel
    different branches: ``FileNotFoundError`` means the pid is gone, while any
    other ``OSError`` means the host would not answer. Only the first may be
    reported as ``exited``.
    """
    pid = os.getpid()
    _fail_path_method(
        monkeypatch,
        "read_text",
        f"/proc/{pid}/stat",
        OSError(5, "Input/output error"),
    )

    state = process_state(pid)

    assert not state.available, (
        f"an unreadable stat file leaves the process unobservable, got {state!r}"
    )
    assert not state.exited, (
        "an unreadable stat file must not be reported as an exited process; "
        f"that answer is reserved for a missing record, got {state!r}"
    )
    assert state.state is None, (
        f"an unobservable process has no state to report, got {state!r}"
    )


def test_a_wchan_read_that_fails_keeps_the_observation_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wait channel is optional evidence: losing it must not lose the record.

    ``wchan`` is the one probe whose failure degrades a *field* rather than
    the observation. The process is still observable, so the record stays
    available and only the wait channel becomes ``None`` -- otherwise a
    transient failure on the least important file would discard the state and
    pid the diagnostic was collected for.
    """
    pid = os.getpid()
    _fail_path_method(
        monkeypatch,
        "read_text",
        f"/proc/{pid}/wchan",
        OSError(5, "Input/output error"),
    )

    state = process_state(pid)

    assert state.available, (
        f"a failed wait-channel read must not discard the observation, got {state!r}"
    )
    assert state.state not in {None, "Z"}, (
        f"the scheduler state comes from stat and must survive, got {state!r}"
    )
    assert state.wchan is None, (
        f"the unreadable wait channel must be reported as no evidence, got {state!r}"
    )


def test_a_stat_file_without_a_state_field_reports_no_state() -> None:
    """A ``stat`` payload with no state token yields no state.

    A truncated read is the shape a raced procfs read leaves behind, and the
    parser must answer ``None`` rather than raising or guessing a state from
    the pid alone. Two truncations reach that answer by different branches:
    the delimiter can be missing entirely, or it can be present with nothing
    after it -- the second is what a read that stopped one byte short leaves
    behind, and it is the branch an assertion on ``""`` alone never reaches.
    """
    assert _state_from_stat("") is None, "an empty stat payload has no state field"
    assert _state_from_stat("1234") is None, (
        "a payload with no closing parenthesized command name has no state to read"
    )
    assert _state_from_stat("1234 (comm)") is None, (
        "a delimiter with no state token after it has no state to read either"
    )


def test_an_unlistable_descriptor_directory_reports_no_child_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directory that cannot be listed yields no pipes, without raising.

    The listing can fail on a process that exited between the state probe and
    this one. The stall that triggered the diagnostic must still be reported,
    so the answer is an empty tuple rather than an exception escaping into the
    failure path.
    """
    pid = os.getpid()
    _fail_path_method(
        monkeypatch,
        "iterdir",
        f"/proc/{pid}/fd",
        OSError(13, "Permission denied"),
    )

    assert not child_pipes(pid), (
        "a descriptor directory that cannot be listed reports no pipes"
    )


def test_a_descriptor_that_vanishes_mid_listing_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One unreadable descriptor entry must not discard the others.

    ``fd`` entries disappear as their processes close them, and a probe that
    aborted on the first one would lose the whole listing exactly when the
    pipeline is tearing down -- the moment the diagnostic matters most.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        _fail_path_method(
            monkeypatch,
            "readlink",
            f"/proc/{os.getpid()}/fd/{read_fd}",
            OSError(5, "Input/output error"),
        )

        pipes = child_pipes(os.getpid())
        reported = {pipe.fd for pipe in pipes}

        assert read_fd not in reported, (
            f"a descriptor whose link cannot be read must be skipped, got {pipes!r}"
        )
        assert write_fd in reported, (
            "skipping one descriptor must not discard the rest of the listing, "
            f"got {pipes!r}"
        )
        survivor = next(pipe for pipe in pipes if pipe.fd == write_fd)
        assert survivor.pending_bytes == 5, (
            f"the surviving descriptor must keep its byte count, got {survivor!r}"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_descriptors_that_cannot_be_opened_report_no_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unopenable procfs descriptor is retained with no byte count.

    The byte count is best-effort, but the descriptor itself is evidence: it
    is what tells a reader which pipe the child held. Dropping the entry would
    hide the pipe; the documented answer is to keep it and report the count as
    unknown.
    """

    def refuse_open(*args: object, **kwargs: object) -> int:
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "open", refuse_open)

    assert _pending_bytes(pathlib.Path("/proc/self/fd/0")) is None, (
        "a descriptor that cannot be opened must report no byte count"
    )


def test_a_failing_read_end_ioctl_reports_no_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ioctl the kernel refuses is unobservable output, not empty output.

    ``FIONREAD`` is not supported on every descriptor -- a directory, an
    ``epoll`` fd, or a closed descriptor all raise. ``None`` is the documented
    "cannot be observed" answer, and it must differ from a count of zero,
    which would read as "the child wrote nothing".
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        _fail_ioctl(monkeypatch, OSError(25, "Inappropriate ioctl for device"))

        assert _pending_bytes(pathlib.Path(f"/proc/self/fd/{read_fd}")) is None, (
            "a refused ioctl must report an unknown byte count, not zero"
        )
        assert read_end_pending_bytes(read_fd) is None, (
            "the parent's own descriptor must report the same unknown count"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_a_runtime_without_fcntl_reports_no_byte_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``fcntl`` is an unobservable count, not an import failure.

    The count is taken through an optional module. On a runtime that lacks it
    -- the helpers are reachable from any host that has procfs -- the probe
    must degrade to no evidence rather than let an ``ImportError`` escape into
    the stall reporting that called it.
    """
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"hello")
        _hide_fcntl_module(monkeypatch)

        assert _pending_bytes(pathlib.Path(f"/proc/self/fd/{read_fd}")) is None, (
            "without fcntl the byte count is unobservable"
        )
        assert read_end_pending_bytes(read_fd) is None, (
            "without fcntl the parent's own descriptor is equally unobservable"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_an_unlistable_self_fd_directory_reports_no_parent_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``/proc/self/fd`` that cannot be listed reports no parent end.

    Both parent probes share this listing, and "no parent end found" is what
    their callers already read as "this edge cannot witness a missed
    hand-off" -- so the degradation is an empty tuple on each, not an error.
    """
    _fail_path_method(
        monkeypatch,
        "iterdir",
        "/proc/self/fd",
        OSError(13, "Permission denied"),
    )

    assert not parent_write_fds("pipe:[1]"), (
        "an unlistable self/fd directory reports no parent writer"
    )
    assert not parent_read_fds("pipe:[1]"), (
        "an unlistable self/fd directory reports no parent read end"
    )


def test_the_parent_probes_need_fcntl_to_read_descriptor_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Identifying a parent end needs ``fcntl``, and losing it reports no end.

    The descriptor *mode* is the whole distinction between a write end and a
    read end, so on a runtime without ``fcntl`` neither probe can answer.
    Both degrade to an empty tuple rather than escaping, for the reason the
    unlistable-directory case gives: an unwitnessable edge is not an error.
    """
    read_fd, write_fd = os.pipe()
    try:
        _hide_fcntl_module(monkeypatch)

        assert not parent_write_fds("pipe:[1]"), (
            "without fcntl a descriptor's mode cannot be read, so no parent "
            "writer can be identified"
        )
        assert not parent_read_fds("pipe:[1]"), (
            "without fcntl a descriptor's mode cannot be read, so no parent "
            "read end can be identified"
        )
    finally:
        os.close(read_fd)
        os.close(write_fd)
