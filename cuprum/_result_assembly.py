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
import typing as typ

from cuprum._pipeline_types import _ExecutionInvariantError

if typ.TYPE_CHECKING:
    from cuprum._rusage import _ChildRusageSnapshot
    from cuprum._subprocess_wait_types import _StreamPayload
    from cuprum.echo_events import RelayFallback


@dc.dataclass(frozen=True, slots=True)
class _RunMeasurements:
    """The measured fields both result classes declare identically.

    These are the keyword arguments the result classes accept after their five
    positional ones, so :meth:`as_kwargs` is the whole of what the two
    constructions share. The rusage snapshot is kept whole rather than
    flattened here, so the ``None`` propagation for the three resource figures
    happens in exactly one place.
    """

    pid: int
    started_at: float
    duration: float
    rusage: _ChildRusageSnapshot | None
    relay_fallbacks: tuple[RelayFallback, ...]

    def as_kwargs(self) -> dict[str, object]:
        """Return these measurements as the results' shared keyword arguments."""
        return {
            "pid": self.pid,
            "started_at": self.started_at,
            "duration": self.duration,
            "max_rss_bytes": None if self.rusage is None else self.rusage.max_rss_bytes,
            "user_cpu_seconds": (
                None if self.rusage is None else self.rusage.user_cpu_seconds
            ),
            "system_cpu_seconds": (
                None if self.rusage is None else self.rusage.system_cpu_seconds
            ),
            "relay_fallbacks": self.relay_fallbacks,
        }


def _require_bytes(payload: _StreamPayload | None, stream: str) -> bytes | None:
    """Narrow a captured payload the run's mode says is byte-exact.

    The widening that lets one drain serve both modes loses the guarantee that
    the payload's type matches the mode the result is being built for, so the
    guarantee is re-established here rather than asserted away with a cast. A
    mismatch is an internal contradiction — a decoded payload arriving from a
    byte-exact config — and saying so is more useful than reporting a
    replacement character as the child's output.

    Returns
    -------
    bytes | None
        The payload, unchanged.

    Raises
    ------
    _ExecutionInvariantError
        If a byte-exact run produced a decoded payload.
    """
    if isinstance(payload, str):
        msg = f"byte-exact run produced text for {stream}"
        raise _ExecutionInvariantError(msg)
    return payload


def _require_text(payload: _StreamPayload | None, stream: str) -> str | None:
    """Narrow a captured payload the run's mode says is text.

    The counterpart of :func:`_require_bytes`, for the ordinary result class.

    Returns
    -------
    str | None
        The payload, unchanged.

    Raises
    ------
    _ExecutionInvariantError
        If a text-mode run produced bytes.
    """
    if isinstance(payload, bytes):
        msg = f"text-mode run produced bytes for {stream}"
        raise _ExecutionInvariantError(msg)
    return payload


def _narrow_payload(
    payload: _StreamPayload | None,
    stream: str,
    *,
    capture_bytes: bool,
) -> bytes | None:
    """Narrow a captured payload as byte-exact, refusing anything already decoded.

    The narrowing counterpart of :func:`_require_bytes` for callers that hold
    the mode as a value rather than having committed to a construction already.
    A pipeline stage reads its mode once and then narrows both of its streams,
    so without this it would have to either restate the check or spread the
    result-class choice over two branches.

    Returns
    -------
    bytes | None
        The payload, unchanged.
    """
    if not capture_bytes:
        return typ.cast("bytes | None", payload)
    return _require_bytes(payload, stream)


__all__ = [
    "_RunMeasurements",
    "_narrow_payload",
    "_require_bytes",
    "_require_text",
]
