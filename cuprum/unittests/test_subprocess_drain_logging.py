"""Tests for what the consumer drain records about what it discards.

The drain runs while a timeout or cancellation is already propagating, so it can
neither raise what it finds nor report it to the caller: a broken reader decodes
to the same text as an empty one. Diagnosis therefore rests entirely on the
DEBUG records asserted here — a reader that failed, and a grace window that
expired with readers still parked.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

from cuprum._subprocess_drain import _CAPTURE_EOF_GRACE_S, _drain_stream_consumers

if typ.TYPE_CHECKING:
    import pytest

_DRAIN_LOGGER = "cuprum._subprocess_drain"


def _field(record: logging.LogRecord, name: str) -> object:
    """Read a structured field the drain attached to a record via ``extra``."""
    return record.__dict__.get(name)


class _ReaderFailureError(RuntimeError):
    """Raised by a stream-consumer double that fails during the drain."""


async def _fails_immediately() -> str | None:
    """Fail as a reader does when its stream breaks rather than ends."""
    await asyncio.sleep(0)
    raise _ReaderFailureError


async def _completes(text: str) -> str | None:
    """Settle promptly with ``text``, as a reader that reached EOF does."""
    await asyncio.sleep(0)
    return text


async def _never_reaches_eof() -> str | None:
    """Block as a reader does on a pipe whose EOF never arrives."""
    await asyncio.Event().wait()
    return None


def test_a_failed_reader_is_recorded_before_it_is_discarded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reader that raised is named at DEBUG, with its exception attached.

    Decoding maps the failure to the same empty string an idle reader reports,
    so without this record a broken pipe and a silent command are the same
    event from outside.
    """

    async def run_case() -> None:
        """Drain a failing stdout reader alongside a healthy stderr one."""
        consumers = (
            asyncio.create_task(_fails_immediately()),
            asyncio.create_task(_completes("err")),
        )
        for _ in range(4):
            await asyncio.sleep(0)
        await _drain_stream_consumers(consumers, capture=True)

    with caplog.at_level(logging.DEBUG, logger=_DRAIN_LOGGER):
        asyncio.run(run_case())

    failures = [
        record
        for record in caplog.records
        if "stream_consumer_failed" in record.message
    ]
    assert len(failures) == 1, (
        f"only the failing reader may be recorded, got {caplog.messages}"
    )
    record = failures[0]
    operation = _field(record, "cuprum_operation")
    assert operation == "drain_stdout", (
        f"the record must name the failing stream, got {operation!r}"
    )
    error_type = _field(record, "cuprum_error_type")
    assert error_type == "_ReaderFailureError", (
        f"the record must name the failure, got {error_type!r}"
    )
    assert record.exc_info is not None, "the record must carry the failure itself"


def test_a_cancelled_reader_is_not_recorded_as_a_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Cancelling a wedged reader is the drain's own doing and says nothing.

    Every teardown cancels whatever is still pending, so recording that would
    make the failure record worthless by making it routine.
    """

    async def run_case() -> None:
        """Drain two readers that never reach EOF under a capturing run."""
        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_never_reaches_eof()),
        )
        await _drain_stream_consumers(consumers, capture=False)

    with caplog.at_level(logging.DEBUG, logger=_DRAIN_LOGGER):
        asyncio.run(run_case())

    assert not [
        record
        for record in caplog.records
        if "stream_consumer_failed" in record.message
    ], f"a plain cancellation must not be recorded as a failure, got {caplog.messages}"


def test_an_expired_grace_window_is_recorded_with_its_pending_readers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Readers still parked when the window closes are counted at DEBUG.

    The window expiring is how a wedged pipe — commonly a grandchild that
    inherited it — shows up, and it costs the run whatever those readers had
    yet to read.
    """

    async def run_case() -> None:
        """Drain one wedged reader and one that reached EOF, under capture."""
        consumers = (
            asyncio.create_task(_never_reaches_eof()),
            asyncio.create_task(_completes("err")),
        )
        await _drain_stream_consumers(consumers, capture=True)

    with caplog.at_level(logging.DEBUG, logger=_DRAIN_LOGGER):
        asyncio.run(run_case())

    expiries = [
        record
        for record in caplog.records
        if "capture_eof_grace_expired" in record.message
    ]
    assert len(expiries) == 1, (
        f"an expired window must be recorded once, got {caplog.messages}"
    )
    record = expiries[0]
    pending = _field(record, "cuprum_pending_readers")
    assert pending == 1, f"the record must count the parked readers, got {pending!r}"
    window = _field(record, "cuprum_timeout_s")
    assert window == _CAPTURE_EOF_GRACE_S, (
        f"the record must state the window, got {window!r}"
    )


def test_readers_that_all_reach_eof_leave_the_window_unrecorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A window that closes with nothing pending is not worth a record."""

    async def run_case() -> None:
        """Drain two readers that both reach EOF inside the window."""
        consumers = (
            asyncio.create_task(_completes("out")),
            asyncio.create_task(_completes("err")),
        )
        await _drain_stream_consumers(consumers, capture=True)

    with caplog.at_level(logging.DEBUG, logger=_DRAIN_LOGGER):
        asyncio.run(run_case())

    assert not caplog.records, (
        f"an uneventful drain must stay quiet, got {caplog.messages}"
    )
