"""Compare dispatch log records against the log specification.

Every record ``_emit_exec_event`` writes is compared here, with its exact
logger, level, message, structured fields, and exception information. The
scheduling and failure records are compared in emission order; the task
lifecycle records are compared as an unordered multiset, because a suspension
can interleave two tasks' records.

:func:`_log_mismatch` is called by ``mismatch`` in
:mod:`_observe_hook_dispatch_compare`, which runs the dispatcher and reports the
first departure from the specification in its fixed order.
"""

from __future__ import annotations

import typing as typ
from itertools import starmap

from cuprum.unittests._observe_hook_dispatch_records import (
    _LIFECYCLE_PREFIXES,
    _LOGGER_NAME,
    DISPATCH_MESSAGE_PREFIXES,
    ExpectedRecord,
    expected_records,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import logging

    from cuprum.events import ExecEvent
    from cuprum.unittests._observe_hook_dispatch_oracle import DispatchExpectation


# The structured fields every dispatch record may carry. Comparing them by this
# fixed list lets lifecycle records be sorted independently of their order.
_RECORD_FIELDS: tuple[str, ...] = (
    "cuprum_phase",
    "cuprum_program",
    "cuprum_error_type",
    "cuprum_scheduled_task_count",
)


def _extra_mismatch(
    label: str,
    record: logging.LogRecord,
    expected: cabc.Mapping[str, object],
) -> str | None:
    """Return the first structured field of ``record`` that ``expected`` rejects."""
    for key, value in expected.items():
        actual = getattr(record, key, None)
        if actual != value:
            return f"{label} {key}={actual!r}, expected {value!r}"
    return None


def _first_mismatch(mismatches: cabc.Iterable[str | None]) -> str | None:
    """Return the first real mismatch, if any."""
    return next((mismatch for mismatch in mismatches if mismatch is not None), None)


def _record_mismatch(
    record: logging.LogRecord,
    expected: ExpectedRecord,
) -> str | None:
    """Check one record's full message, level, extras, and exception type."""
    if record.name != _LOGGER_NAME:
        return f"record logger {record.name!r}, expected {_LOGGER_NAME!r}"
    if record.levelno != expected.level:
        return f"record level {record.levelno}, expected {expected.level}"
    message = record.getMessage()
    if message != expected.message:
        return f"record message {message!r}, expected {expected.message!r}"
    if record.exc_info is None:
        if expected.exc_type is not None:
            return f"record must carry {expected.exc_type.__name__} in exc_info"
    elif record.exc_info[0] is not expected.exc_type:
        # ``exc_info[0]`` is optional, so name whatever type is actually there.
        actual_name = getattr(record.exc_info[0], "__name__", None)
        return (
            f"record exc_info {actual_name}, "
            f"expected {getattr(expected.exc_type, '__name__', None)!r}"
        )
    return _extra_mismatch("record", record, expected.extra)


def _record_key(
    record: logging.LogRecord,
) -> tuple[int, str, tuple[tuple[str, object], ...]]:
    """Return a canonical key, for order-insensitive comparison."""
    return (
        record.levelno,
        record.getMessage(),
        tuple((key, getattr(record, key, None)) for key in _RECORD_FIELDS),
    )


def _expected_key(
    expected: ExpectedRecord,
) -> tuple[int, str, tuple[tuple[str, object], ...]]:
    """Return the canonical key of an expected record."""
    return (
        expected.level,
        expected.message,
        tuple((key, expected.extra.get(key)) for key in _RECORD_FIELDS),
    )


def _dispatch_records(
    records: cabc.Iterable[logging.LogRecord],
) -> list[logging.LogRecord]:
    """Return the records the dispatcher owns, in emission order."""
    return [
        record
        for record in records
        if record.name == _LOGGER_NAME
        and record.getMessage().startswith(DISPATCH_MESSAGE_PREFIXES)
    ]


def _lifecycle_mismatch(
    records: cabc.Sequence[logging.LogRecord],
    expected: cabc.Sequence[ExpectedRecord],
) -> str | None:
    """Check the task lifecycle records as an unordered multiset of full records.

    A task's records may interleave with another's, so their order is not part
    of the contract; sorting pairs each record with the specification entry it
    stands for. The sort key deliberately covers only the level, the message,
    and the structured fields, so the pairing cannot hide a difference in what
    the key leaves out: each pair is compared again in full, which covers the
    exception information a failure record must carry.

    Returns
    -------
    str | None
        The first lifecycle record that departs from the specification, or
        ``None``.
    """
    actual_keys = sorted(map(_record_key, records))
    expected_keys = sorted(map(_expected_key, expected))
    if actual_keys != expected_keys:
        return "the task lifecycle records do not match the specification"
    paired = zip(
        sorted(records, key=_record_key),
        sorted(expected, key=_expected_key),
        strict=True,
    )
    return _first_mismatch(starmap(_record_mismatch, paired))


def _log_mismatch(
    records: cabc.Iterable[logging.LogRecord],
    expectation: DispatchExpectation,
    event: ExecEvent,
) -> str | None:
    """Check every dispatch record against the specification."""
    actual = _dispatch_records(records)
    ordered_expected, lifecycle_expected = expected_records(expectation, event)
    ordered_actual = [
        record
        for record in actual
        if not record.getMessage().startswith(_LIFECYCLE_PREFIXES)
    ]
    lifecycle_actual = [
        record
        for record in actual
        if record.getMessage().startswith(_LIFECYCLE_PREFIXES)
    ]
    if len(ordered_actual) != len(ordered_expected):
        return (
            f"{len(ordered_actual)} scheduling/failure records, "
            f"expected {len(ordered_expected)}"
        )
    ordered = _first_mismatch(
        starmap(_record_mismatch, zip(ordered_actual, ordered_expected, strict=True))
    )
    if ordered is not None:
        return ordered
    return _lifecycle_mismatch(lifecycle_actual, lifecycle_expected)
