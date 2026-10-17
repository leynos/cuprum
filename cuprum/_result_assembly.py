"""The rules a finished execution applies when reporting its result.

Two result classes exist, and both are built by these rules:

- :class:`cuprum.sh.CommandResult` reports decoded text;
- :class:`cuprum.sh.BytesCommandResult` reports the child's bytes untouched.

They differ only in their two captured-output fields, which leaves two seams
worth naming rather than repeating at every construction site:

- :func:`_require_bytes` and :func:`_require_text` narrow a drain's widened
  ``str | bytes`` capture back to the type the chosen class declares. Sharing
  one drain between the two modes costs that guarantee, and the result is the
  one place a mode/type contradiction could otherwise escape unnoticed — so the
  guarantee is re-established rather than asserted away.
- :class:`_RunMeasurements` carries the fields every result declares
  identically, so a figure added to one construction and forgotten in the other
  cannot become a silent difference between a text run and a binary run of the
  same command.

Both the single-command path (``cuprum._subprocess_execution``) and the
per-stage pipeline path (``cuprum._pipeline_results``) build results, so both
read these rules from here; a stage and a direct run of the same command should
differ only in what could actually be measured.
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
    "_require_text",
]
