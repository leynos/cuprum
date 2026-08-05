"""Structured logging for inter-stage hops that decline the Rust pump.

Every decline is silent by construction: the hop still completes on the Python
pump, so no caller-visible signal distinguishes a deployment that has quietly
stopped taking the fast path from one that never had it. These tests pin the
three decline reasons to the real code paths that emit them, rather than
calling the log helper directly, so a decline that stops being recorded fails
here. The paths outnumber the reasons: the blocking seam can refuse in two
ways, and both must be attributed to the same reason rather than one of them
escaping as an exception.
"""

from __future__ import annotations

import logging
import typing as typ

import pytest

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
