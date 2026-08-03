"""Timeout and teardown telemetry reporting.

Owns the two channels a timeout or teardown failure is reported on — the
structured ``cuprum.timeout`` log record and the ``timeout`` /
``teardown_error`` observe events — and the ``_report_*`` helpers that pair
them so the channels cannot drift.

This lives apart from ``cuprum._subprocess_timeout`` because the reporting
surface is shared: the single-command wait path and the pipeline deadline path
both report through it, and the pipeline caller cannot import the
timeout-translation module without closing an import cycle. Every emission
here is best-effort and must never displace the exception it describes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import typing as typ

from cuprum._pipeline_types import _EventDetails

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum._pipeline_types import _StageObservation

# Timeout diagnostics ride the module-scoped ``cuprum.timeout`` logger,
# mirroring the ``cuprum.stdin`` convention in ``cuprum._subprocess_stdin``:
# structured ``cuprum_*`` extra fields let observability integrations key on
# stable attributes rather than parsing the human-readable message.
_LOGGER = logging.getLogger("cuprum.timeout")

# Distinguishes an elapsed wall-clock deadline from an immediate expiry forced
# by a non-positive (``<= 0``) timeout, which cannot await the process at all.
# These are the stable ``timeout_mode`` values on both the ``cuprum.timeout``
# log records and the ``timeout`` observe event.
type _TimeoutMode = typ.Literal["elapsed_deadline", "non_positive_immediate"]


def _emit_timeout_log(
    level: int,
    msg: str,
    *args: object,
    extra: dict[str, object],
) -> None:
    """Emit a timeout diagnostic that can never mask the timeout it describes.

    Telemetry is strictly best-effort here: a logging failure (for example a
    misconfigured handler that raises) must not replace the ``TimeoutError`` or
    ``TimeoutExpired`` the caller is about to raise, nor abort teardown, so any
    exception raised while logging is swallowed. Preserving cleanup and
    exception precedence outranks emitting the diagnostic.
    """
    with contextlib.suppress(Exception):
        _LOGGER.log(level, msg, *args, extra=extra)


def _log_timeout_expiry(
    *,
    pid: int | None,
    configured_timeout: float,
    mode: _TimeoutMode,
) -> None:
    """Record a structured diagnostic when a subprocess timeout expires.

    ``mode`` distinguishes an elapsed wall-clock deadline
    (``"elapsed_deadline"``) from the deterministic immediate expiry taken for a
    non-positive timeout (``"non_positive_immediate"``).
    """
    _emit_timeout_log(
        logging.WARNING,
        "subprocess_timeout_expired pid=%s timeout=%s mode=%s",
        pid,
        configured_timeout,
        mode,
        extra={
            "cuprum_pid": pid,
            "cuprum_operation": "wait",
            "cuprum_timeout_s": configured_timeout,
            "cuprum_timeout_mode": mode,
            "cuprum_error_type": TimeoutError.__name__,
        },
    )


def _log_teardown_drain_failure(*, pid: int | None, joined: str) -> None:
    """Record a structured diagnostic when teardown drains a failing consumer.

    Called when cancelling and draining stream consumers surfaces an unexpected
    exception (anything other than the expected ``CancelledError``). Reports the
    teardown outcome so a drain failure is observable even though it is absorbed
    to preserve the primary timeout or cancellation. ``joined`` is the
    comma-joined error-class list prepared by
    :func:`_report_teardown_drain_failure`.
    """
    _emit_timeout_log(
        logging.ERROR,
        "subprocess_teardown_drain_failed pid=%s errors=%s",
        pid,
        joined,
        extra={
            "cuprum_pid": pid,
            "cuprum_operation": "teardown",
            "cuprum_teardown_outcome": "drain_error",
            "cuprum_error_type": joined,
        },
    )


def _safe_emit(
    observation: _StageObservation,
    phase: typ.Literal["timeout", "teardown_error"],
    details: _EventDetails,
) -> None:
    """Emit an observe event best-effort so telemetry cannot mask a failure.

    An observe-hook failure — which :meth:`_StageObservation.emit` re-raises —
    must never replace the ``TimeoutExpired`` / ``CancelledError`` the caller is
    about to raise, nor abort teardown. ``emit`` records any scheduled
    async-hook tasks on the observation *before* raising, so those are still
    drained by the runner; only the synchronous hook failure is swallowed here.
    Preserving the primary exception and cleanup precedence outranks emitting
    the observe event.
    """
    # Swallow a synchronous observe-hook failure (including a hook raising
    # CancelledError) so it cannot replace the timeout/cancellation.
    with contextlib.suppress(Exception, asyncio.CancelledError):
        observation.emit(phase, details)


def _emit_timeout_event(
    observation: _StageObservation,
    *,
    pid: int | None,
    configured_timeout: float,
    mode: _TimeoutMode,
) -> None:
    """Best-effort ``timeout`` observe event describing an expiry.

    Carries the stable fields callers use to distinguish an elapsed deadline
    from an immediate non-positive expiry: ``operation="wait"``, ``pid``,
    ``timeout_s`` (the configured timeout), ``error_type="TimeoutError"``, and
    ``timeout_mode``. Emitted before the preserved ``exit`` event and
    ``TimeoutExpired``.
    """
    _safe_emit(
        observation,
        "timeout",
        _EventDetails(
            pid=pid,
            operation="wait",
            error_type=TimeoutError.__name__,
            timeout_s=configured_timeout,
            timeout_mode=mode,
        ),
    )


def _emit_teardown_error_event(
    observation: _StageObservation,
    *,
    pid: int | None,
    joined: str,
) -> None:
    """Best-effort ``teardown_error`` observe event for a consumer drain failure.

    Carries ``operation="drain"``, ``pid``, and ``error_type`` (``joined``, the
    comma-joined failure classes prepared by
    :func:`_report_teardown_drain_failure`). The failure is absorbed to preserve
    the primary timeout or cancellation, but stays observable through this event.
    """
    _safe_emit(
        observation,
        "teardown_error",
        _EventDetails(
            pid=pid,
            operation="drain",
            error_type=joined,
            note=f"consumer drain failed: {joined}",
        ),
    )


def _report_timeout_expiry(
    observation: _StageObservation,
    *,
    pid: int | None,
    configured_timeout: float,
    mode: _TimeoutMode,
) -> None:
    """Report an expiry through both timeout telemetry channels.

    The structured ``cuprum.timeout`` log record and the ``timeout`` observe
    event always travel together and carry the same fields, so every expiry
    route reports through here rather than repeating the pair — the
    single-command wait path and the pipeline deadline path alike. Both
    emissions are best-effort and cannot mask the timeout they describe.
    """
    _log_timeout_expiry(pid=pid, configured_timeout=configured_timeout, mode=mode)
    _emit_timeout_event(
        observation,
        pid=pid,
        configured_timeout=configured_timeout,
        mode=mode,
    )


def _report_pipeline_timeout_expiry(
    observations: tuple[_StageObservation, ...],
    processes: cabc.Sequence[asyncio.subprocess.Process],
    *,
    configured_timeout: float | None,
) -> None:
    """Report a pipeline deadline expiry on both timeout telemetry channels.

    A pipeline deadline is enforced once for the whole run rather than per
    stage, but its events are per stage like every other pipeline phase, so
    each stage reports its own ``pid``. That keeps a pipeline expiry legible on
    the same channels as a single-command expiry instead of leaving it silent.

    ``configured_timeout`` is ``None`` only when no deadline was set, in which
    case there is nothing to report. Reporting is best-effort throughout and
    runs while a ``TimeoutExpired`` is already propagating, so nothing raised
    here may escape and displace it.
    """
    if configured_timeout is None:
        return
    mode: _TimeoutMode = (
        "non_positive_immediate" if configured_timeout <= 0 else "elapsed_deadline"
    )
    with contextlib.suppress(Exception):
        for observation, process in zip(observations, processes, strict=False):
            _report_timeout_expiry(
                observation,
                pid=process.pid,
                configured_timeout=configured_timeout,
                mode=mode,
            )


def _report_teardown_drain_failure(
    observation: _StageObservation | None,
    *,
    pid: int | None,
    error_types: tuple[str, ...],
) -> None:
    """Report a consumer drain failure through both teardown telemetry channels.

    Mirrors :func:`_report_timeout_expiry`: the comma-joined error-class list is
    built once and shared by the structured ``cuprum.timeout`` log record and the
    ``teardown_error`` observe event. The event is emitted only when the caller
    has an observation to emit it on. Both emissions are best-effort and cannot
    mask the failure being cleaned up after.
    """
    joined = ",".join(error_types)
    _log_teardown_drain_failure(pid=pid, joined=joined)
    if observation is not None:
        _emit_teardown_error_event(observation, pid=pid, joined=joined)


__all__ = [
    "_LOGGER",
    "_TimeoutMode",
    "_emit_teardown_error_event",
    "_emit_timeout_event",
    "_emit_timeout_log",
    "_log_teardown_drain_failure",
    "_log_timeout_expiry",
    "_report_pipeline_timeout_expiry",
    "_report_teardown_drain_failure",
    "_report_timeout_expiry",
    "_safe_emit",
]
