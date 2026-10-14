"""The rules for building text and byte results from completed executions.

Three result classes describe the output contract:

- :class:`cuprum.sh.CommandResult` reports decoded text, with ``None`` for a
  stream that was not captured.
- :class:`cuprum.sh.CapturedResult` reports captured single-command text and
  guarantees both streams are strings, including ``""`` for empty output.
- :class:`cuprum.sh.BytesCommandResult` reports the child's bytes untouched.

The stream drain can widen payloads to ``str | bytes``, so
:func:`_require_bytes` and :func:`_require_text` restore the type required by
the selected mode. The single-command path uses
:func:`_require_captured_text` when capture is enabled; it rejects a missing
stream before a :class:`cuprum.sh.CapturedResult` can be built. Pipeline
assembly uses the broader text and byte checks because an interior stage's
stdout may be streamed to the next process instead of captured.

:class:`_RunMeasurements` carries the fields all three result classes declare
identically, so adding a measurement to one construction cannot silently
change the result reported for another output mode. Both the single-command
path (``cuprum._subprocess_execution``) and the per-stage pipeline path
(``cuprum._pipeline_results``) use these rules; their stream fields differ
only where the execution mode requires it.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import typing as typ

from cuprum._pipeline_types import _ExecutionInvariantError
from cuprum.events import CaptureMode

if typ.TYPE_CHECKING:
    from cuprum._rusage import ChildResourceUsage, _ChildRusageSnapshot
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.capture_policy import CapturedStream
    from cuprum.echo_events import RelayFallback

_LOGGER = logging.getLogger(__name__)


class _RunMeasurementsKwargs(typ.TypedDict):
    """The keyword arguments :meth:`_RunMeasurements.as_kwargs` returns.

    Declared rather than left as ``dict[str, object]`` so unpacking it into a
    result constructor keeps each field's type: an opaque value type would
    make every ``**kwargs`` argument unrepresentable to the type checker, and
    the measurements would stop being checked against the fields they fill.
    """

    pid: int
    started_at: float
    duration: float
    max_rss_bytes: int | None
    user_cpu_seconds: float | None
    system_cpu_seconds: float | None
    resolved_path: str | None
    relay_fallbacks: tuple[RelayFallback, ...]


@dc.dataclass(frozen=True, slots=True)
class _RunMeasurements:
    """The measured fields both result classes declare identically.

    These are the keyword arguments the result classes accept after their five
    positional ones, so :meth:`as_kwargs` is the whole of what the two
    constructions share. The rusage is kept whole rather than flattened here,
    so the ``None`` propagation for the three resource figures happens in
    exactly one place.

    ``rusage`` accepts either of the two shapes the measurement seam produces:
    the pre-spawn snapshot a ``wait4`` bracket takes, or the aggregate
    :func:`~cuprum._wait4_spawn.resource_usage_for` falls back to when the
    direct child cannot be reaped through that interface. Both report the same
    three figures and both allow a missing ``max_rss_bytes``, so accepting the
    union here is what lets the caller pass the measurement on rather than
    convert it.

    ``resolved_path`` belongs here rather than at either construction: it is
    the executable the run actually reached, which both modes report and
    neither derives from the captured streams.
    """

    pid: int
    started_at: float
    duration: float
    rusage: _ChildRusageSnapshot | ChildResourceUsage | None
    resolved_path: str | None
    relay_fallbacks: tuple[RelayFallback, ...]

    def as_kwargs(self) -> _RunMeasurementsKwargs:
        """Return these measurements as the results' shared keyword arguments."""
        rusage = self.rusage
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "duration": self.duration,
            "max_rss_bytes": None if rusage is None else rusage.max_rss_bytes,
            "user_cpu_seconds": (None if rusage is None else rusage.user_cpu_seconds),
            "system_cpu_seconds": (
                None if rusage is None else rusage.system_cpu_seconds
            ),
            "resolved_path": self.resolved_path,
            "relay_fallbacks": self.relay_fallbacks,
        }


def _require_mode_payload[T: (str, bytes)](
    payload: _StreamPayload | None,
    stream: str,
    expected: type[T],
    mode: CaptureMode,
) -> T | None:
    """Narrow a captured payload to the type *mode* promises for *stream*.

    The widening that lets one drain serve both modes loses the guarantee that
    the payload's type matches the mode the result is being built for, so the
    guarantee is re-established here rather than asserted away with a cast. A
    mismatch is an internal contradiction — a decoded payload arriving from a
    byte-exact config — and saying so is more useful than reporting a
    replacement character as the child's output.

    The mismatch is also logged before it is raised. The exception reaches a
    caller who may well be catching broadly, while a mode/type contradiction
    means one of the two is wrong for every run down that path, which is exactly
    what an operator needs in the record rather than only in a caught traceback.

    Returns
    -------
    T | None
        The payload, unchanged.

    Raises
    ------
    _ExecutionInvariantError
        If the payload's type contradicts the run's capture mode.
    """
    if payload is not None and not isinstance(payload, expected):
        actual = type(payload).__name__
        # A mode is named ``text``/``bytes``; the sentence below reads "text
        # mode", and the log field keeps the operator's exact filter value.
        _LOGGER.error(
            "capture mode contradicts the captured payload: "
            "capture_mode=%s stream=%s expected=%s actual=%s",
            mode,
            stream,
            expected.__name__,
            actual,
        )
        msg = f"{mode} mode run produced {actual} for {stream}"
        raise _ExecutionInvariantError(msg)

    return payload


def _require_bytes(payload: _StreamPayload | None, stream: str) -> bytes | None:
    """Narrow a captured payload the run's mode says is byte-exact.

    Returns
    -------
    bytes | None
        The payload, unchanged.
    """
    return _require_mode_payload(payload, stream, bytes, CaptureMode.BYTES)


def _require_text(payload: _StreamPayload | None, stream: str) -> str | None:
    """Narrow a captured payload the run's mode says is text.

    Returns
    -------
    str | None
        The payload, unchanged.
    """
    return _require_mode_payload(payload, stream, str, CaptureMode.TEXT)


def _require_captured_text(
    payload: _StreamPayload | None,
    stream: str,
    *,
    program: str,
    exec_id: str,
) -> str:
    """Require a decoded stream when a single-command run captured output."""
    text = _require_text(payload, stream)
    if text is None:
        _LOGGER.error(
            "captured result assembly found a missing text stream: "
            "operation=%s capture_mode=%s stream=%s program=%s exec_id=%s",
            "single_command_result_assembly",
            CaptureMode.TEXT,
            stream,
            program,
            exec_id,
        )
        msg = f"text capture was enabled but {stream} was not captured"
        raise _ExecutionInvariantError(msg)
    return text


def _recorded_captures(
    captures: tuple[CapturedStream | None, CapturedStream | None],
) -> tuple[CapturedStream, ...]:
    """Flatten a run's per-stream capture records into its result's tuple.

    A result reports one record per stream it actually captured, in
    stdout-then-stderr order, and nothing for a stream it did not. Draining the
    optional pair here rather than at each construction site is what keeps
    ``CommandResult.captures`` from being ``(None, None)`` for a run that
    captured one stream and ``(record,)`` for a run that captured both — the
    shape a caller would have to know the config to unpick.

    Parameters
    ----------
    captures : tuple[CapturedStream | None, CapturedStream | None]
        The stdout and stderr records, either of which is ``None`` when that
        stream was not captured.

    Returns
    -------
    tuple[CapturedStream, ...]
        The records that exist, in stream order.

    Examples
    --------
    A run that captured only stderr reports one record::

        _recorded_captures((None, stderr_record))
    """
    return tuple(record for record in captures if record is not None)


__all__ = [
    "_RunMeasurements",
    "_recorded_captures",
    "_require_bytes",
    "_require_captured_text",
    "_require_text",
]
