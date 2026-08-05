"""Structured logging for inter-stage hops that decline the Rust pump.

Every decline is silent by construction: the hop still completes on the Python
pump, so no caller-visible signal distinguishes a deployment that has quietly
stopped taking the fast path from one that never had it. These tests pin the
three decline reasons to the real code paths that emit them, rather than
calling the log helper directly, so a decline that stops being recorded fails
here. The paths outnumber the reasons: the blocking seam can refuse in two
ways, and both must be attributed to the same reason rather than one of them
escaping as an exception.

The teardown steps that undo the hand-off are covered on the same terms. Each
is deliberately suppressed — a failure there must not displace whatever the hop
was already reporting — but suppression without a record makes a step that has
quietly stopped working indistinguishable from one that never had to run.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import typing as typ

import pytest

from cuprum import _pipeline_streams
from cuprum._pipeline_stream_fds import (
    RUST_PUMP_TEARDOWN_FAILED_ACTION,
    _paused_reader,
    _restore_stream_fd_blocking,
)
from cuprum.adapters.pump_metrics import PumpMetricsHook
from cuprum.pump_observation import observe_pump
from cuprum.unittests._rust_pump_test_helpers import (
    DECLINE_PATHS,
    RecordingCollector,
    decline_on_pause_failure,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER_NAME = "cuprum._pipeline_streams"
_FDS_LOGGER_NAME = "cuprum._pipeline_stream_fds"


def _teardown_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured fields of every suppressed teardown failure."""
    return [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == RUST_PUMP_TEARDOWN_FAILED_ACTION
    ]


def _assert_teardown_record(
    fields: dict[str, object],
    *,
    site: str,
    error_type: str,
) -> None:
    """Assert one teardown record names its site, its error, and DEBUG."""
    assert fields["cuprum_site"] == site, (
        f"the record must name the step that failed, expected {site!r}, "
        f"found {fields['cuprum_site']!r}"
    )
    assert fields["cuprum_error_type"] == error_type, (
        f"expected {error_type!r}, found {fields['cuprum_error_type']!r}"
    )
    assert fields["levelno"] == logging.DEBUG, (
        "a suppressed teardown step is a diagnostic, not a fault; it must stay "
        f"at DEBUG, found {fields['levelname']}"
    )


def _decline_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured fields of every recorded Rust-pump decline."""
    return [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_declined"
    ]


@pytest.mark.parametrize(
    ("trigger", "expected_reason"),
    [(trigger, reason) for _id, trigger, reason in DECLINE_PATHS],
    ids=[path_id for path_id, _trigger, _reason in DECLINE_PATHS],
)
def test_declining_the_rust_pump_records_its_reason(
    trigger: cabc.Callable[[pytest.MonkeyPatch], None],
    expected_reason: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each fall-back path names why the hop declined the Rust pump."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        trigger(monkeypatch)

    records = _decline_records(caplog)
    assert len(records) == 1, (
        f"expected exactly one decline record for {expected_reason!r}, "
        f"found {len(records)}"
    )
    assert records[0]["cuprum_reason"] == expected_reason, (
        f"expected the decline to be attributed to {expected_reason!r}, "
        f"found {records[0]['cuprum_reason']!r}"
    )
    # Asserted per reason rather than once: falling back is a routing decision
    # rather than a fault, and a single-path check would miss a regression that
    # promoted only one of them above DEBUG, making a working pipeline
    # noisy on every platform where the fast path does not apply.
    assert records[0]["levelno"] == logging.DEBUG, (
        f"{expected_reason!r} must be recorded at DEBUG, found "
        f"{records[0]['levelname']}"
    )


def test_a_registered_observer_does_not_displace_the_log_record(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The DEBUG record survives the arrival of the metrics channel.

    The counters supplement these records; an operator whose alerting reads the
    log pipeline must not lose it because someone registered a pump hook.
    """
    collector = RecordingCollector()
    with (
        caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME),
        observe_pump(PumpMetricsHook(collector)),
    ):
        decline_on_pause_failure(monkeypatch)

    records = _decline_records(caplog)
    assert len(records) == 1, (
        f"the DEBUG record must survive alongside the counter, found {len(records)}"
    )
    assert collector.counter_names() == ["cuprum_rust_pump_declined_total"], (
        f"the counter must be recorded too, found {collector.counter_names()}"
    )


class _UnresumableTransport:
    """A reader transport that pauses successfully but cannot be resumed."""

    def __init__(self) -> None:
        """Start with no pause recorded."""
        self.paused = False

    def pause_reading(self) -> None:
        """Pause read callbacks, as a healthy transport would."""
        self.paused = True

    def resume_reading(self) -> None:
        """Fail the way a transport whose loop has closed would."""
        raise OSError(errno.EIO, "the transport can no longer be resumed")


class _FakeReader:
    """A stream reader exposing only the transport the pause seam reads."""

    def __init__(self, transport: _UnresumableTransport) -> None:
        """Hold the transport ``_pause_reader_transport`` will find."""
        self.transport = transport


class _FailingWriter:
    """A stream writer whose close fails with an error nothing else handles."""

    def write_eof(self) -> None:
        """Refuse with a bare ``OSError``, which the close helper re-raises."""
        raise OSError(errno.EBADF, "the descriptor is already gone")


def test_a_reader_that_cannot_be_resumed_is_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A resume that fails on block exit is suppressed but not silent.

    The reader was paused for the pump's benefit, so a failed resume leaves
    asyncio not reading a descriptor it owns. Raising here would displace
    whatever the block was already unwinding, which is why it is suppressed —
    and why the record is the only evidence it happened.
    """
    transport = _UnresumableTransport()
    reader = typ.cast("asyncio.StreamReader", _FakeReader(transport))

    with (
        caplog.at_level(logging.DEBUG, logger=_FDS_LOGGER_NAME),
        _paused_reader(reader) as may_hand_off,
    ):
        assert may_hand_off is True, (
            "a transport that pauses must permit the hand-off, or the resume "
            "under test never runs"
        )
        assert transport.paused, "the pause must actually have been applied"

    records = _teardown_records(caplog)
    assert len(records) == 1, (
        f"a failed resume must be recorded exactly once, found {len(records)}"
    )
    _assert_teardown_record(records[0], site="resume", error_type="OSError")
    assert records[0]["cuprum_errno"] == errno.EIO, (
        f"the record must carry the errno, found {records[0]['cuprum_errno']!r}"
    )


def test_descriptors_that_cannot_be_restored_are_recorded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Restoring the blocking mode of closed descriptors is recorded per FD.

    Both halves are attempted regardless, so the writer's restore is not
    skipped because the reader's failed; both failures are therefore reported.
    """
    reader_fd, writer_fd = os.pipe()
    os.close(reader_fd)
    os.close(writer_fd)

    with caplog.at_level(logging.DEBUG, logger=_FDS_LOGGER_NAME):
        _restore_stream_fd_blocking(
            reader_fd=reader_fd,
            writer_fd=writer_fd,
            reader_was_blocking=True,
            writer_was_blocking=True,
        )

    records = _teardown_records(caplog)
    assert len(records) == 2, (
        "both descriptors must be attempted and both failures recorded, found "
        f"{len(records)}"
    )
    for fields in records:
        _assert_teardown_record(
            fields,
            site="restore_blocking",
            error_type="OSError",
        )


def test_a_writer_close_that_fails_after_the_pump_is_recorded(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-pump writer close records the error it swallows.

    The Rust pump closes the writer descriptor itself, so ``EBADF`` here is
    expected and must not fail the hop. Without a record, a close that failed
    for any other reason is indistinguishable from that expected one.
    """

    # RUF029: the seam this replaces is awaited by `_run_rust_pump`, so the
    # double must be a coroutine function even though it awaits nothing.
    async def handled(**_kwargs: object) -> bool:  # noqa: RUF029
        """Report that the Rust pump took the hop."""
        return True

    monkeypatch.setattr(_pipeline_streams, "_pump_over_raw_fds", handled)
    writer = typ.cast("asyncio.StreamWriter", _FailingWriter())

    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        result = asyncio.run(
            _pipeline_streams._run_rust_pump(
                reader=typ.cast("asyncio.StreamReader", object()),
                writer=writer,
                reader_fd=-1,
                writer_fd=-1,
            )
        )

    assert result is True, (
        "a failed writer close must not turn a completed Rust hop into a "
        f"fall-back, found {result!r}"
    )
    records = _teardown_records(caplog)
    assert len(records) == 1, (
        f"the swallowed close error must be recorded once, found {len(records)}"
    )
    _assert_teardown_record(records[0], site="writer_close", error_type="OSError")
