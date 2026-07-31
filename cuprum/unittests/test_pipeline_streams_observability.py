"""Structured logging for inter-stage hops that decline the Rust pump.

Every decline is silent by construction: the hop still completes on the Python
pump, so no caller-visible signal distinguishes a deployment that has quietly
stopped taking the fast path from one that never had it. These tests pin the
three decline reasons to the real code paths that emit them, rather than
calling the log helper directly, so a decline that stops being recorded fails
here.
"""

from __future__ import annotations

import asyncio
import logging
import typing as typ

import pytest

from cuprum import _pipeline_stream_fds, _pipeline_streams

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_LOGGER_NAME = "cuprum._pipeline_streams"


def _decline_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, object]]:
    """Return the structured fields of every recorded Rust-pump decline."""
    return [
        record.__dict__
        for record in caplog.records
        if record.__dict__.get("cuprum_action") == "rust_pump_declined"
    ]


def _fail_engage(**_kwargs: object) -> object:
    """Refuse to switch the descriptors to blocking mode."""
    msg = "blocking mode is unavailable for this descriptor pair"
    raise OSError(msg)


def _decline_on_missing_fds() -> None:
    """Route a hop whose streams expose no raw descriptors."""
    reader = typ.cast("asyncio.StreamReader", object())
    asyncio.run(_pipeline_streams._try_rust_pump(reader, None))


def _decline_on_pause_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a hop whose reader transport cannot be paused."""
    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=False),
    )
    _run_raw_fd_pump()


def _decline_on_blocking_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route a hop whose descriptors cannot be made blocking."""
    monkeypatch.setattr(
        _pipeline_streams,
        "_pause_reader_transport",
        lambda _reader: _pipeline_stream_fds._ReaderPause(may_hand_off=True),
    )
    monkeypatch.setattr(_pipeline_stream_fds._BlockingModeGuard, "engage", _fail_engage)
    _run_raw_fd_pump()


def _run_raw_fd_pump() -> None:
    """Drive ``_pump_over_raw_fds`` over placeholder descriptors."""
    reader = typ.cast("asyncio.StreamReader", object())
    asyncio.run(
        _pipeline_streams._pump_over_raw_fds(
            reader=reader,
            writer=None,
            reader_fd=1,
            writer_fd=2,
        )
    )


@pytest.mark.parametrize(
    ("trigger", "expected_reason"),
    [
        (lambda _monkeypatch: _decline_on_missing_fds(), "raw_fd_unavailable"),
        (_decline_on_pause_failure, "reader_pause_failed"),
        (_decline_on_blocking_failure, "blocking_mode_unavailable"),
    ],
    ids=["missing_fds", "pause_failure", "blocking_failure"],
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
    # Asserted per reason so a regression that promotes a single fall-back
    # path above DEBUG does not hide behind the other two cases.
    assert records[0]["levelno"] == logging.DEBUG, (
        f"{expected_reason!r} must be recorded at DEBUG, found "
        f"{records[0]['levelname']}"
    )
