"""Property tests for the timeout telemetry pairing invariant.

Timeout diagnostics travel on two channels — a structured ``cuprum.timeout``
log record and an ``ExecEvent`` observe event — funnelled through
``_report_timeout_expiry`` and ``_report_teardown_drain_failure`` so the pair
cannot drift apart. The per-channel tests in
``test_subprocess_timeout_logging`` and ``test_subprocess_timeout_observe``
each pin one channel for one fixed case; these check the cross-channel
invariant over generated inputs: whatever is reported, both channels carry the
same facts.
"""

from __future__ import annotations

import contextlib
import logging
import typing as typ

from hypothesis import given
from hypothesis import strategies as st

from cuprum._subprocess_execution import _report_timeout_expiry
from cuprum._subprocess_timeout import _report_teardown_drain_failure
from cuprum.unittests._timeout_test_helpers import _RecordingObservation

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_TIMEOUT_LOGGER = "cuprum.timeout"

_PIDS = st.none() | st.integers(min_value=1, max_value=99_999)
_TIMEOUTS = st.floats(
    min_value=-3600.0, max_value=3600.0, allow_nan=False, allow_infinity=False
)
_MODES = st.sampled_from(("elapsed_deadline", "non_positive_immediate"))
_ERROR_TYPES = st.lists(
    st.sampled_from(("ValueError", "OSError", "RuntimeError")),
    min_size=1,
    max_size=3,
)


class _RecordCollector(logging.Handler):
    """Handler collecting emitted records for one generated example."""

    def __init__(self, records: list[logging.LogRecord]) -> None:
        """Collect into ``records``."""
        super().__init__()
        self._records = records

    @typ.override
    def emit(self, record: logging.LogRecord) -> None:
        """Append ``record`` to the collected list."""
        self._records.append(record)


@contextlib.contextmanager
def _capture_timeout_records(level: int) -> cabc.Iterator[list[logging.LogRecord]]:
    """Capture ``cuprum.timeout`` records, isolated per generated example.

    Hypothesis does not reset function-scoped fixtures between examples, so
    ``caplog`` would accumulate across them; this attaches and detaches its own
    handler for each call instead.
    """
    records: list[logging.LogRecord] = []
    logger = logging.getLogger(_TIMEOUT_LOGGER)
    handler = _RecordCollector(records)
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(level)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _sole_record(records: list[logging.LogRecord]) -> logging.LogRecord:
    """Return the single ``cuprum.timeout`` record captured for one example."""
    assert len(records) == 1, (
        f"reporting must emit exactly one {_TIMEOUT_LOGGER} record, "
        f"got {[rec.getMessage() for rec in records]}"
    )
    return records[0]


@given(pid=_PIDS, configured_timeout=_TIMEOUTS, mode=_MODES)
def test_timeout_expiry_reports_agree_across_channels(
    pid: int | None,
    configured_timeout: float,
    mode: str,
) -> None:
    """The timeout log record and observe event carry identical facts.

    Whatever pid, configured timeout, and mode are reported, both channels must
    fire exactly once and agree field for field, so a consumer reading either
    channel draws the same conclusion about the expiry.
    """
    observation = _RecordingObservation()

    with _capture_timeout_records(logging.WARNING) as records:
        _report_timeout_expiry(
            typ.cast("typ.Any", observation),
            pid=pid,
            configured_timeout=configured_timeout,
            mode=typ.cast("typ.Any", mode),
        )

    fields = vars(_sole_record(records))
    emitted = [details for phase, details in observation.events if phase == "timeout"]
    assert len(emitted) == 1, (
        f"reporting must emit exactly one timeout observe event, "
        f"got {observation.events!r}"
    )
    details = emitted[0]
    for log_key, event_value in (
        ("cuprum_pid", details.pid),
        ("cuprum_timeout_s", details.timeout_s),
        ("cuprum_timeout_mode", details.timeout_mode),
        ("cuprum_error_type", details.error_type),
        ("cuprum_operation", details.operation),
    ):
        assert fields[log_key] == event_value, (
            f"the log record's {log_key}={fields[log_key]!r} must match the "
            f"observe event's corresponding field {event_value!r}"
        )


@given(pid=_PIDS, error_types=_ERROR_TYPES)
def test_teardown_drain_reports_agree_across_channels(
    pid: int | None,
    error_types: list[str],
) -> None:
    """The teardown log record and observe event carry the same failure facts.

    ``operation`` deliberately differs between the channels (the log names the
    ``teardown`` stage, the event names the ``drain`` operation), so the shared
    facts are the pid and the comma-joined error classes; both must be built
    from one join rather than computed twice.
    """
    observation = _RecordingObservation()

    with _capture_timeout_records(logging.ERROR) as records:
        _report_teardown_drain_failure(
            typ.cast("typ.Any", observation),
            pid=pid,
            error_types=tuple(error_types),
        )

    fields = vars(_sole_record(records))
    emitted = [
        details for phase, details in observation.events if phase == "teardown_error"
    ]
    assert len(emitted) == 1, (
        f"reporting must emit exactly one teardown_error observe event, "
        f"got {observation.events!r}"
    )
    details = emitted[0]
    assert fields["cuprum_pid"] == details.pid, (
        f"the log record's pid {fields['cuprum_pid']!r} must match the observe "
        f"event's pid {details.pid!r}"
    )
    assert fields["cuprum_error_type"] == details.error_type, (
        f"both channels must report the same joined error classes, got log "
        f"{fields['cuprum_error_type']!r} and event {details.error_type!r}"
    )
    assert details.error_type == ",".join(error_types), (
        f"the joined error classes must preserve the reported order, got "
        f"{details.error_type!r} for {error_types!r}"
    )
