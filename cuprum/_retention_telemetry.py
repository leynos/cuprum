"""Bounded, structured telemetry for observe-hook task retention.

Retention decisions are only useful to an operator if they can be told apart
after the fact: "this run was slow" and "this run paused its child because its
observers could not keep up" look the same in a bare duration. This module owns
the recorder that distinguishes them. It holds counters only — the admission
timeline of one stage — and writes fixed-field log records and bounded metric
samples; the rules the counters describe live in
:mod:`cuprum._observe_task_retention`.

Every field is a number, a closed phase name, or the execution's correlation
token. No command text, decoded line, or error message is recorded, so log
volume and label cardinality stay bounded no matter what a child prints.
"""

from __future__ import annotations

import dataclasses as dc
import logging
import time
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from cuprum.events import ExecId

_LOGGER = logging.getLogger(__name__)

#: Sample kinds. Each is a closed label value a metrics adapter may project.
_RETENTION_DEPTH = "depth"
_RETENTION_SATURATION = "saturation"
_RETENTION_PRUNE = "prune"
_RETENTION_CAPACITY_WAIT = "capacity_wait"

#: Wait outcomes. A wait ends when capacity frees, when its caller is
#: cancelled, or when an unrelated failure unwinds through it.
_WAIT_RESUMED = "resumed"
_WAIT_CANCELLED = "cancelled"
_WAIT_INTERRUPTED = "interrupted"


@dc.dataclass(frozen=True, slots=True)
class _RetentionSample:
    """One bounded retention observation offered to a metrics adapter.

    Attributes
    ----------
    exec_id:
        Correlation token shared with the stage's execution events.
    kind:
        One of the closed ``_RETENTION_*`` constants.
    phase:
        Closed event-phase or stream name, or ``None`` for a hand-built case.
    depth:
        Unfinished observer tasks when the sample was taken, when known.
    pruned:
        Tasks dropped by the prune this sample describes.
    pruned_failures:
        Failed or cancelled tasks dropped because an earlier failure is kept.
    wait_seconds:
        Measured duration of the capacity wait this sample describes.
    outcome:
        Closed wait outcome, for capacity-wait samples only.

    """

    exec_id: ExecId
    kind: str
    phase: str | None = None
    depth: int = 0
    pruned: int = 0
    pruned_failures: int = 0
    wait_seconds: float = 0.0
    outcome: str | None = None


@typ.runtime_checkable
class _RetentionMetricsSink(typ.Protocol):
    """Port for an adapter that projects retention samples into metrics."""

    def report_retention(self, sample: _RetentionSample) -> None:
        """Project one bounded retention observation into metrics."""


@dc.dataclass(slots=True)
class _RetentionTelemetry:
    """Bounded counters and structured logs for one execution's retention.

    One recorder belongs to each stage observation. The counters describe the
    admission decisions that stage made, while ``depth`` and ``max_depth`` read
    the shared retention list and therefore describe the run. Every field is a
    number, a closed phase name, or the execution's correlation token: no
    command text, decoded line, or error message is recorded, so the log
    volume and label cardinality stay bounded.

    Attributes
    ----------
    exec_id:
        Correlation token shared with the stage's execution events.
    sinks:
        Adapters that receive each bounded sample as metrics.
    clock:
        Monotonic clock the capacity-wait durations are measured through. It is
        a seam rather than a direct ``time.perf_counter`` call so a test can
        drive a wait's recorded duration deterministically.
    depth:
        Unfinished observer tasks at the most recent observation.
    max_depth:
        Highest unfinished depth seen for this stage.
    admissions:
        Admission decisions this stage made.
    saturations:
        Admission decisions refused because the ceiling was reached.
    pruned:
        Settled tasks dropped by in-place pruning.
    pruned_failures:
        Failed or cancelled tasks dropped because an earlier failure is kept.
    waits:
        Capacity waits that suspended the line read loop.
    wait_seconds:
        Total monotonic time spent suspended in capacity waits.

    """

    exec_id: ExecId
    sinks: tuple[_RetentionMetricsSink, ...] = ()
    clock: cabc.Callable[[], float] = time.perf_counter
    depth: int = 0
    max_depth: int = 0
    admissions: int = 0
    saturations: int = 0
    pruned: int = 0
    pruned_failures: int = 0
    waits: int = 0
    wait_seconds: float = 0.0

    def record_depth(self, unfinished: int, *, phase: str | None) -> int:
        """Record the unfinished-task depth a caller already counted."""
        self.depth = unfinished
        self.max_depth = max(self.max_depth, unfinished)
        self._publish(
            _RetentionSample(
                exec_id=self.exec_id,
                kind=_RETENTION_DEPTH,
                phase=phase,
                depth=unfinished,
            )
        )
        return unfinished

    def record_admission(
        self,
        *,
        admitted: bool,
        ceiling: int,
        phase: str | None,
        retained: int,
    ) -> None:
        """Record one admission decision at debug level."""
        self.admissions += 1
        if not admitted:
            self.saturations += 1
            self._publish(
                _RetentionSample(
                    exec_id=self.exec_id,
                    kind=_RETENTION_SATURATION,
                    phase=phase,
                    depth=self.depth,
                )
            )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "observe_task_admission admitted=%s phase=%s ceiling=%s "
                "retained=%s depth=%s max_depth=%s admissions=%s saturations=%s",
                admitted,
                phase,
                ceiling,
                retained,
                self.depth,
                self.max_depth,
                self.admissions,
                self.saturations,
                extra=self._fields(
                    phase=phase,
                    ceiling=ceiling,
                    admitted=admitted,
                    retained=retained,
                ),
            )

    def record_prune(
        self,
        *,
        pruned: int,
        pruned_failures: int,
        ceiling: int,
        phase: str | None,
    ) -> None:
        """Record one in-place prune of the shared retention list."""
        self.pruned += pruned
        self.pruned_failures += pruned_failures
        self._publish(
            _RetentionSample(
                exec_id=self.exec_id,
                kind=_RETENTION_PRUNE,
                phase=phase,
                depth=self.depth,
                pruned=pruned,
                pruned_failures=pruned_failures,
            )
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "observe_task_pruned pruned=%s pruned_failures=%s phase=%s "
                "ceiling=%s depth=%s max_depth=%s",
                pruned,
                pruned_failures,
                phase,
                ceiling,
                self.depth,
                self.max_depth,
                extra=self._fields(
                    phase=phase,
                    ceiling=ceiling,
                    prune_delta=pruned,
                    prune_failures_delta=pruned_failures,
                ),
            )

    def log_capacity_wait(self, *, ceiling: int, phase: str | None) -> None:
        """Log the transition into observer-task backpressure."""
        _LOGGER.info(
            "observe_task_capacity_wait phase=%s ceiling=%s depth=%s max_depth=%s",
            phase,
            ceiling,
            self.depth,
            self.max_depth,
            extra=self._fields(phase=phase, ceiling=ceiling),
        )

    def record_wait(
        self,
        *,
        elapsed: float,
        ceiling: int,
        phase: str | None,
        outcome: str = _WAIT_RESUMED,
    ) -> None:
        """Record one completed capacity wait and its duration."""
        self.waits += 1
        self.wait_seconds += elapsed
        self._publish(
            _RetentionSample(
                exec_id=self.exec_id,
                kind=_RETENTION_CAPACITY_WAIT,
                phase=phase,
                depth=self.depth,
                wait_seconds=elapsed,
                outcome=outcome,
            )
        )
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "observe_task_capacity_resumed phase=%s ceiling=%s outcome=%s "
                "elapsed_s=%.6f waits=%s wait_seconds=%.6f",
                phase,
                ceiling,
                outcome,
                elapsed,
                self.waits,
                self.wait_seconds,
                extra=self._fields(
                    phase=phase,
                    ceiling=ceiling,
                    outcome=outcome,
                    elapsed_s=elapsed,
                ),
            )

    def _publish(self, sample: _RetentionSample) -> None:
        """Offer one bounded sample to every installed metrics sink."""
        for sink in self.sinks:
            sink.report_retention(sample)

    def _fields(
        self,
        *,
        phase: str | None,
        ceiling: int,
        **extra: object,
    ) -> dict[str, object]:
        """Build the fixed structured-log fields shared by every record.

        Callers pass the per-record values by their bare names; they are
        prefixed here so every field this module logs shares one namespace and
        a consumer can select them without colliding with ``LogRecord``'s own
        attributes. The prefix also keeps a per-prune delta from overwriting the
        running total it belongs to.

        Returns
        -------
        dict[str, object]
            The fixed fields plus the prefixed per-record values.
        """
        fields: dict[str, object] = {
            "cuprum_exec_id": str(self.exec_id),
            "cuprum_phase": phase,
            "cuprum_ceiling": ceiling,
            "cuprum_depth": self.depth,
            "cuprum_max_depth": self.max_depth,
            "cuprum_admissions": self.admissions,
            "cuprum_saturations": self.saturations,
            "cuprum_pruned": self.pruned,
            "cuprum_pruned_failures": self.pruned_failures,
            "cuprum_waits": self.waits,
            "cuprum_wait_seconds": self.wait_seconds,
        }
        fields.update({f"cuprum_{key}": value for key, value in extra.items()})
        return fields


__all__ = [
    "_RETENTION_CAPACITY_WAIT",
    "_RETENTION_DEPTH",
    "_RETENTION_PRUNE",
    "_RETENTION_SATURATION",
    "_WAIT_CANCELLED",
    "_WAIT_INTERRUPTED",
    "_WAIT_RESUMED",
    "_RetentionMetricsSink",
    "_RetentionSample",
    "_RetentionTelemetry",
]
