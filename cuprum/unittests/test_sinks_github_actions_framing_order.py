"""Framing order for buffered custom GitHub Actions destinations.

Child echo writes raw bytes through ``log.buffer`` and flushes that buffer
itself, so a non-line-buffered text wrapper would otherwise let child output
reach the runner before the protective workflow-command frame. These tests
inspect the underlying byte buffer rather than the text layer, because that is
the stream the runner actually reads.
"""

from __future__ import annotations

import typing as typ

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from cuprum.sinks import (
    GitHubActionsSink,
    SessionOutcome,
    SessionStart,
    TerminalOutcome,
)
from tests.helpers.github_actions_frames import buffered_log

if typ.TYPE_CHECKING:
    from cuprum.sinks.github_actions import GitHubActionsSession

_LEASE_ID = "0123456789abcdef"
_GROUP_OPEN = "::group::project: program\n"
_STOP_FRAME = f"::stop-commands::{_LEASE_ID}\n"
_RESUME = f"::{_LEASE_ID}::\n"
_ENDGROUP = "::endgroup::\n"
_ANNOTATION = "::error title=project%3A program::exit_nonzero\n"
_TERMINAL = f"{_RESUME}{_ENDGROUP}"


@pytest.fixture(autouse=True)
def _fixed_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every session's stop token so expected frames are exact."""
    monkeypatch.setattr(
        "cuprum.sinks.github_actions._new_stop_token",
        lambda: _LEASE_ID,
    )


def _open_session(sink: GitHubActionsSink) -> GitHubActionsSession:
    """Open one forced-active session, asserting the sink activated."""
    session = sink.open_session(SessionStart(label="project: program", argv=()))
    assert session is not None, "a forced sink must return an active session"
    return session


def test_buffered_log_receives_opening_frames_before_return() -> None:
    """Opening framing is flushed to the byte buffer, not left in the wrapper."""
    log, raw = buffered_log()
    sink = GitHubActionsSink(log, force=True)

    _open_session(sink)

    expected = f"{_GROUP_OPEN}{_STOP_FRAME}".encode()
    assert raw.getvalue() == expected, (
        f"opening frames must reach the byte buffer unflushed-free; "
        f"got {raw.getvalue()!r}"
    )


def test_buffered_log_receives_successful_terminal_frames_before_return() -> None:
    """A successful close flushes the lease release and the endgroup."""
    log, raw = buffered_log()
    session = _open_session(GitHubActionsSink(log, force=True))

    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    expected = f"{_GROUP_OPEN}{_STOP_FRAME}{_TERMINAL}".encode()
    assert raw.getvalue() == expected, (
        f"terminal frames must reach the byte buffer on close; got {raw.getvalue()!r}"
    )


def test_buffered_log_receives_failure_annotation_before_return() -> None:
    """A failed close flushes the annotation after the terminal frame."""
    log, raw = buffered_log()
    session = _open_session(GitHubActionsSink(log, force=True))

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    expected = f"{_GROUP_OPEN}{_STOP_FRAME}{_TERMINAL}{_ANNOTATION}".encode()
    assert raw.getvalue() == expected, (
        f"the annotation must reach the byte buffer on close; got {raw.getvalue()!r}"
    )


def test_annotation_only_close_flushes_without_opening_frames() -> None:
    """A group-less failure writes and flushes exactly one annotation."""
    log, raw = buffered_log()
    sink = GitHubActionsSink(log, force=True, emit_group=False)
    session = _open_session(sink)

    assert raw.getvalue() == b"", "a group-less session writes no opening frame"

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    assert raw.getvalue() == _ANNOTATION.encode(), (
        f"the annotation-only close must flush its one frame; got {raw.getvalue()!r}"
    )


class _RecordingLog:
    """Record writes and flushes in order, optionally failing one flush."""

    def __init__(self, *, fail_flush_number: int | None = None) -> None:
        """Start with no recorded events, failing the configured flush."""
        self.events: list[tuple[str, str]] = []
        self._flushes = 0
        self._fail_flush_number = fail_flush_number

    def write(self, text: str, /) -> int:
        """Record one text write."""
        self.events.append(("write", text))
        return len(text)

    def flush(self) -> None:
        """Record one flush, or raise when this is the configured failure."""
        self._flushes += 1
        if self._flushes == self._fail_flush_number:
            raise BrokenPipeError
        self.events.append(("flush", ""))


_TERMINAL_EVENTS = (
    ("write", _RESUME),
    ("write", _ENDGROUP),
    ("flush", ""),
)
_ANNOTATION_EVENTS = (("write", _ANNOTATION), ("flush", ""))


@pytest.mark.parametrize(
    ("framing", "outcome", "expected_events"),
    [
        pytest.param(
            (True, False),
            TerminalOutcome.EXIT_NONZERO,
            _TERMINAL_EVENTS,
            id="annotations-disabled",
        ),
        pytest.param(
            (True, True),
            TerminalOutcome.EXIT_ZERO,
            _TERMINAL_EVENTS,
            id="successful-outcome",
        ),
        pytest.param(
            (False, False),
            TerminalOutcome.EXIT_NONZERO,
            (),
            id="failure-without-frames",
        ),
        pytest.param(
            (False, True),
            TerminalOutcome.EXIT_NONZERO,
            _ANNOTATION_EVENTS,
            id="annotation-only",
        ),
        pytest.param(
            (True, True),
            TerminalOutcome.EXIT_NONZERO,
            _TERMINAL_EVENTS + _ANNOTATION_EVENTS,
            id="framed-failure",
        ),
    ],
)
def test_close_flushes_every_framing_block_it_writes(
    framing: tuple[bool, bool],
    outcome: TerminalOutcome,
    expected_events: tuple[tuple[str, str], ...],
) -> None:
    """Each close path writes its frames and then flushes them once.

    The event order is the contract: a flush that preceded the writes, or one
    per line rather than one per framing block, would publish part of a
    multi-line frame ahead of the rest of it.
    """
    emit_group, emit_annotation = framing
    log = _RecordingLog()
    sink = GitHubActionsSink(
        typ.cast("typ.IO[str]", log),
        force=True,
        emit_group=emit_group,
        emit_annotation=emit_annotation,
    )
    session = _open_session(sink)
    log.events.clear()
    exit_code = 0 if outcome is TerminalOutcome.EXIT_ZERO else 1

    session.close(SessionOutcome(outcome, exit_code=exit_code))

    assert log.events == list(expected_events), (
        f"close must flush once per framing block it writes; "
        f"got {log.events!r}, expected {expected_events!r}"
    )


def test_flush_failure_propagates_and_second_close_writes_nothing() -> None:
    """A flush failure aborts close, and the closed session stays silent."""
    log = _RecordingLog(fail_flush_number=2)
    sink = GitHubActionsSink(typ.cast("typ.IO[str]", log), force=True)
    session = _open_session(sink)
    opened_events = list(log.events)

    with pytest.raises(BrokenPipeError):
        session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    session.close(SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1))

    terminal_writes = [
        ("write", f"::{session.stop_token}::\n"),
        ("write", _ENDGROUP),
    ]
    assert log.events == opened_events + terminal_writes, (
        f"the failed close must write the terminal block once and never "
        f"annotate; got {log.events!r}"
    )


@settings(max_examples=50, deadline=None)
@given(payload=st.binary(max_size=256))
def test_arbitrary_child_payload_stays_inside_the_frames(payload: bytes) -> None:
    """Every child payload sits between the opening frames and the terminal ones.

    The payload is arbitrary binary, so the frames cannot be located by
    searching the output for them: a payload may itself contain
    workflow-command text. The contract is positional — the opening frames end
    where the payload begins, and the terminal frames follow every payload
    byte. Child echo is modelled by writing to and flushing the byte buffer,
    which is exactly what ``log.buffer`` offers it.
    """
    log, raw = buffered_log()
    session = _open_session(GitHubActionsSink(log, force=True))

    raw.write(payload)
    raw.flush()
    session.close(SessionOutcome(TerminalOutcome.EXIT_ZERO, exit_code=0))

    opening = f"{_GROUP_OPEN}{_STOP_FRAME}".encode()
    terminal = _TERMINAL.encode()
    data = raw.getvalue()
    assert data.startswith(opening), (
        f"child output must not precede the opening frames; got {data[:64]!r}"
    )
    start = len(opening)
    end = start + len(payload)
    assert data[start:end] == payload, (
        f"child bytes must follow the opening frames unchanged; "
        f"got {data[start:end]!r} for payload {payload!r}"
    )
    assert data[end:] == terminal, (
        f"the lease release and endgroup must follow all child bytes; "
        f"got {data[end:]!r}"
    )


@settings(max_examples=60, deadline=None)
@given(
    framing=st.sampled_from(((True, True), (True, False), (False, True))),
    fail_index=st.integers(min_value=1, max_value=2),
)
def test_failed_flush_aborts_close_at_any_block(
    framing: tuple[bool, bool],
    fail_index: int,
) -> None:
    """A close-time flush failure stops the close at that block, for good.

    ``fail_index`` counts close-time flushes, and the framing pair decides how
    many of them a close performs, so every reachable failure point is
    covered. The block whose flush failed keeps its writes in the recorder —
    the failure happens after them — but no later block is attempted, and the
    closed session never writes again on a second close.
    """
    emit_group, emit_annotation = framing
    blocks = [
        block
        for emit, block in (
            (emit_group, _TERMINAL_EVENTS),
            (emit_annotation, _ANNOTATION_EVENTS),
        )
        if emit
    ]
    assume(fail_index <= len(blocks))

    # The opening block also flushes, so the recorder's fail point is offset
    # by it: only a group-enabled session flushes on open.
    log = _RecordingLog(fail_flush_number=int(emit_group) + fail_index)
    sink = GitHubActionsSink(
        typ.cast("typ.IO[str]", log),
        force=True,
        emit_group=emit_group,
        emit_annotation=emit_annotation,
    )
    session = _open_session(sink)
    log.events.clear()

    outcome = SessionOutcome(TerminalOutcome.EXIT_NONZERO, exit_code=1)
    with pytest.raises(BrokenPipeError):
        session.close(outcome)

    attempted = list(log.events)
    session.close(outcome)

    expected = [event for block in blocks[: fail_index - 1] for event in block] + list(
        blocks[fail_index - 1][:-1]
    )
    assert attempted == expected, (
        f"close must stop at the {fail_index}th flush, having published every "
        f"earlier block and the failed block's writes; expected {expected!r}, "
        f"got {attempted!r}"
    )
    assert log.events == attempted, (
        f"a closed session must never write again; got {log.events!r}"
    )
